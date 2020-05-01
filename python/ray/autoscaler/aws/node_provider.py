from collections import defaultdict
import itertools
import logging
import operator
import random
import threading

import boto3
import botocore
from botocore.config import Config

from ray.autoscaler.node_provider import NodeProvider
from ray.autoscaler.tags import TAG_RAY_CLUSTER_NAME, TAG_RAY_NODE_NAME, \
    TAG_RAY_LAUNCH_CONFIG, TAG_RAY_NODE_TYPE
from ray.ray_constants import BOTO_MAX_RETRIES, BOTO_CREATE_MAX_RETRIES
from ray.autoscaler.log_timer import LogTimer

logger = logging.getLogger(__name__)


def to_aws_format(tags):
    """Convert the Ray node name tag to the AWS-specific 'Name' tag."""

    if TAG_RAY_NODE_NAME in tags:
        tags["Name"] = tags[TAG_RAY_NODE_NAME]
        del tags[TAG_RAY_NODE_NAME]
    return tags


def from_aws_format(tags):
    """Convert the AWS-specific 'Name' tag to the Ray node name tag."""

    if "Name" in tags:
        tags[TAG_RAY_NODE_NAME] = tags["Name"]
        del tags["Name"]
    return tags


def make_ec2_client(region, max_retries, aws_credentials=None):
    """Make client, retrying requests up to `max_retries`."""
    config = Config(retries={"max_attempts": max_retries})
    aws_credentials = aws_credentials or {}
    return boto3.resource(
        "ec2", region_name=region, config=config, **aws_credentials)


class AWSNodeProvider(NodeProvider):
    def __init__(self, provider_config, cluster_name):
        NodeProvider.__init__(self, provider_config, cluster_name)
        self.cache_stopped_nodes = provider_config.get("cache_stopped_nodes",
                                                       True)
        aws_credentials = provider_config.get("aws_credentials")

        self.ec2 = make_ec2_client(
            region=provider_config["region"],
            max_retries=BOTO_MAX_RETRIES,
            aws_credentials=aws_credentials)
        self.ec2_fail_fast = make_ec2_client(
            region=provider_config["region"],
            max_retries=0,
            aws_credentials=aws_credentials)

        # Try availability zones round-robin, starting from random offset
        self.subnet_idx = random.randint(0, 100)

        self.tag_cache = {}  # Tags that we believe to actually be on EC2.
        self.tag_cache_pending = {}  # Tags that we will soon upload.
        self.tag_cache_lock = threading.Lock()
        self.tag_cache_update_event = threading.Event()
        self.tag_cache_kill_event = threading.Event()
        self.tag_update_thread = threading.Thread(
            target=self._node_tag_update_loop)
        self.tag_update_thread.start()

        # Cache of node objects from the last nodes() call. This avoids
        # excessive DescribeInstances requests.
        self.cached_nodes = {}

    def _node_tag_update_loop(self):
        """Update the AWS tags for a cluster periodically.

        The purpose of this loop is to avoid excessive EC2 calls when a large
        number of nodes are being launched simultaneously.
        """
        while True:
            self.tag_cache_update_event.wait()
            self.tag_cache_update_event.clear()

            batch_updates = defaultdict(list)

            with self.tag_cache_lock:
                for node_id, tags in self.tag_cache_pending.items():
                    for x in tags.items():
                        batch_updates[x].append(node_id)
                    self.tag_cache[node_id].update(tags)

                self.tag_cache_pending = {}

            for (k, v), node_ids in batch_updates.items():
                m = "Set tag {}={} on {}".format(k, v, node_ids)
                with LogTimer("AWSNodeProvider: {}".format(m)):
                    if k == TAG_RAY_NODE_NAME:
                        k = "Name"
                    self.ec2.meta.client.create_tags(
                        Resources=node_ids,
                        Tags=[{
                            "Key": k,
                            "Value": v
                        }],
                    )

            self.tag_cache_kill_event.wait(timeout=5)
            if self.tag_cache_kill_event.is_set():
                return

    def non_terminated_nodes(self, tag_filters):
        # Note that these filters are acceptable because they are set on
        #       node initialization, and so can never be sitting in the cache.
        tag_filters = to_aws_format(tag_filters)
        filters = [
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running"],
            },
            {
                "Name": "tag:{}".format(TAG_RAY_CLUSTER_NAME),
                "Values": [self.cluster_name],
            },
        ]
        for k, v in tag_filters.items():
            filters.append({
                "Name": "tag:{}".format(k),
                "Values": [v],
            })

        nodes = list(self.ec2.instances.filter(Filters=filters))
        # Populate the tag cache with initial information if necessary
        for node in nodes:
            if node.id in self.tag_cache:
                continue

            self.tag_cache[node.id] = from_aws_format(
                {x["Key"]: x["Value"]
                 for x in node.tags})

        self.cached_nodes = {node.id: node for node in nodes}
        return [node.id for node in nodes]

    def is_running(self, node_id):
        node = self._get_cached_node(node_id)
        return node.state["Name"] == "running"

    def is_terminated(self, node_id):
        node = self._get_cached_node(node_id)
        state = node.state["Name"]
        return state not in ["running", "pending"]

    def node_tags(self, node_id):
        with self.tag_cache_lock:
            d1 = self.tag_cache[node_id]
            d2 = self.tag_cache_pending.get(node_id, {})
            return dict(d1, **d2)

    def external_ip(self, node_id):
        node = self._get_cached_node(node_id)

        if node.public_ip_address is None:
            node = self._get_node(node_id)

        return node.public_ip_address

    def internal_ip(self, node_id):
        node = self._get_cached_node(node_id)

        if node.private_ip_address is None:
            node = self._get_node(node_id)

        return node.private_ip_address

    def set_node_tags(self, node_id, tags):
        with self.tag_cache_lock:
            try:
                self.tag_cache_pending[node_id].update(tags)
            except KeyError:
                self.tag_cache_pending[node_id] = tags

            self.tag_cache_update_event.set()

    def create_node(self, node_config, tags, count):
        # Try to reuse previously stopped nodes with compatible configs
        if self.cache_stopped_nodes:
            filters = [
                {
                    "Name": "instance-state-name",
                    "Values": ["stopped", "stopping"],
                },
                {
                    "Name": "tag:{}".format(TAG_RAY_CLUSTER_NAME),
                    "Values": [self.cluster_name],
                },
                {
                    "Name": "tag:{}".format(TAG_RAY_NODE_TYPE),
                    "Values": [tags[TAG_RAY_NODE_TYPE]],
                },
                {
                    "Name": "tag:{}".format(TAG_RAY_LAUNCH_CONFIG),
                    "Values": [tags[TAG_RAY_LAUNCH_CONFIG]],
                },
            ]

            reuse_nodes = list(
                self.ec2.instances.filter(Filters=filters))[:count]
            reuse_node_ids = [n.id for n in reuse_nodes]
            if reuse_nodes:
                logger.info("AWSNodeProvider: reusing instances {}. "
                            "To disable reuse, set "
                            "'cache_stopped_nodes: False' in the provider "
                            "config.".format(reuse_node_ids))

                for node in reuse_nodes:
                    self.tag_cache[node.id] = from_aws_format(
                        {x["Key"]: x["Value"]
                         for x in node.tags})
                    if node.state["Name"] == "stopping":
                        logger.info("AWSNodeProvider: waiting for instance "
                                    "{} to fully stop...".format(node.id))
                        node.wait_until_stopped()

                self.ec2.meta.client.start_instances(
                    InstanceIds=reuse_node_ids)
                for node_id in reuse_node_ids:
                    self.set_node_tags(node_id, tags)
                count -= len(reuse_node_ids)

        if count:
            self._create_node(node_config, tags, count)

    def _create_node(self, node_config, tags, count):
        tags = to_aws_format(tags)
        conf = node_config.copy()

        # Delete unsupported keys from the node config
        try:
            del conf["Resources"]
        except KeyError:
            pass

        tag_specs = self._generate_tag_specs(conf, tags)

        # SubnetIds is not a real config key: we must resolve to a
        # single SubnetId before invoking the AWS API.
        subnet_ids = conf.pop("SubnetIds")

        for attempt in range(1, BOTO_CREATE_MAX_RETRIES + 1):
            try:
                subnet_id = subnet_ids[self.subnet_idx % len(subnet_ids)]
                logger.info("NodeProvider: calling create_instances "
                            "with {} (count={}).".format(subnet_id, count))
                self.subnet_idx += 1
                conf.update({
                    "MinCount": 1,
                    "MaxCount": count,
                    "SubnetId": subnet_id,
                    "TagSpecifications": tag_specs
                })
                created = self.ec2_fail_fast.create_instances(**conf)
                for instance in created:
                    logger.info("NodeProvider: Created instance "
                                "[id={}, name={}, info={}]".format(
                                    instance.instance_id,
                                    instance.state["Name"],
                                    instance.state_reason["Message"]))
                break
            except botocore.exceptions.ClientError as exc:
                if attempt == BOTO_CREATE_MAX_RETRIES:
                    logger.error(
                        "create_instances: Max attempts ({}) exceeded.".format(
                            BOTO_CREATE_MAX_RETRIES))
                    raise exc
                else:
                    logger.error(exc)

    def _generate_tag_specs(self, node_config, user_tags):
        conf = node_config.copy()

        instance_tag_sources = itertools.chain(
            # Tags from config
            conf.get("tags", {}).items(),
            # Tags provided on create_node
            user_tags.items(),
            # Tags we need set for the autoscaler to track the cluster
            [(TAG_RAY_CLUSTER_NAME, self.cluster_name)],
        )

        # Reshape our various tags into an EC2 TagSpecification
        base_tag_spec = [{
            "ResourceType": "instance",
            "Tags": [{
                "Key": k,
                "Value": v
            } for k, v in instance_tag_sources],
        }]

        # Pull out the AWS Provider TagSpecification and make sure it won't
        # break any cluster tags we depend on.
        user_tag_spec = conf.get("TagSpecifications", [])
        self._validate_user_tag_spec(user_tag_spec)

        # Merge the user_tag_spec into our base_tag_spec preferring the more
        # specific UserTag spec values to our default set.
        return self._merge_tag_specs(base_tag_spec, user_tag_spec)

    @staticmethod
    def _validate_user_tag_spec(tag_spec):
        """
        Make a best effort attempt to avoid letting a user's TagSpecification
        overwrite tags the AWS Provider depends on.
        """
        instance_tag_specs = (spec for spec in tag_spec
                              if spec["ResourceType"] == "instance")

        instance_tag_keys = (
            tag["Key"]
            # unnest the tags so we can check for any unwated tag keys
            for instance_specs in instance_tag_specs
            for tag in instance_specs["Tags"])

        CRITICAL_TAGS = [
            TAG_RAY_CLUSTER_NAME, TAG_RAY_NODE_NAME, TAG_RAY_LAUNCH_CONFIG,
            TAG_RAY_NODE_TYPE
        ]

        conflicting_tags = [
            key for key in instance_tag_keys if key in CRITICAL_TAGS
        ]

        assert not any(conflicting_tags), \
            "User provided TagSpecification tried to overwrite tags " \
            "the AWS Node Provider depends on for tracking instances: {}".format(conflicting_tags)

    @staticmethod
    def _merge_tag_specs(left, right):
        """
        Merge the `Tags` associated with each `ResourceType`. Any duplciates
        will be resolved with a preference for `right` over `left`.
        """

        def tag_spec_to_tree(tag_spec):
            return {
                spec["ResourceType"]:
                {t["Key"]: t["Value"]
                 for t in spec["Tags"]}
                for spec in tag_spec
            }

        def tree_to_tag_spec(tree):
            return [{
                "ResourceType": _type,
                "Tags": [{
                    "Key": k,
                    "Value": v
                } for k, v in tags.items()],
            } for _type, tags in tree.items()]

        left_tree = tag_spec_to_tree(left)
        right_tree = tag_spec_to_tree(right)

        merged = {
            _type: {
                **left_tree.get(_type, {}),
                **right_tree.get(_type, {}),
            }
            for _type in set(left_tree) | set(right_tree)
        }

        return AWSNodeProvider._canonical_tag_spec(tree_to_tag_spec(merged))

    @staticmethod
    def _canonical_tag_spec(spec):
        """
        Makes sure any TagSpecification is consistent by sorting all its
        values.
        """
        sorted_tags = ({
            "ResourceType": spec["ResourceType"],
            "Tags": sorted(
                spec["Tags"],
                key=operator.itemgetter("Key"),
            )
        } for spec in spec)

        return sorted(sorted_tags, key=operator.itemgetter("ResourceType"))

    def terminate_node(self, node_id):
        node = self._get_cached_node(node_id)
        if self.cache_stopped_nodes:
            if node.spot_instance_request_id:
                logger.info(
                    "AWSNodeProvider: terminating node {} (spot nodes cannot "
                    "be stopped, only terminated)".format(node_id))
                node.terminate()
            else:
                logger.info(
                    "AWSNodeProvider: stopping node {}. To terminate nodes "
                    "on stop, set 'cache_stopped_nodes: False' in the "
                    "provider config.".format(node_id))
                node.stop()
        else:
            node.terminate()

        self.tag_cache.pop(node_id, None)
        self.tag_cache_pending.pop(node_id, None)

    def terminate_nodes(self, node_ids):
        if not node_ids:
            return
        if self.cache_stopped_nodes:
            spot_ids = []
            on_demand_ids = []

            for node_id in node_ids:
                if self._get_cached_node(node_id).spot_instance_request_id:
                    spot_ids += [node_id]
                else:
                    on_demand_ids += [node_id]

            if on_demand_ids:
                logger.info(
                    "AWSNodeProvider: stopping nodes {}. To terminate nodes "
                    "on stop, set 'cache_stopped_nodes: False' in the "
                    "provider config.".format(on_demand_ids))
                self.ec2.meta.client.stop_instances(InstanceIds=on_demand_ids)
            if spot_ids:
                logger.info(
                    "AWSNodeProvider: terminating nodes {} (spot nodes cannot "
                    "be stopped, only terminated)".format(spot_ids))
                self.ec2.meta.client.terminate_instances(InstanceIds=spot_ids)
        else:
            self.ec2.meta.client.terminate_instances(InstanceIds=node_ids)

        for node_id in node_ids:
            self.tag_cache.pop(node_id, None)
            self.tag_cache_pending.pop(node_id, None)

    def _get_node(self, node_id):
        """Refresh and get info for this node, updating the cache."""
        self.non_terminated_nodes({})  # Side effect: updates cache

        if node_id in self.cached_nodes:
            return self.cached_nodes[node_id]

        # Node not in {pending, running} -- retry with a point query. This
        # usually means the node was recently preempted or terminated.
        matches = list(self.ec2.instances.filter(InstanceIds=[node_id]))
        assert len(matches) == 1, "Invalid instance id {}".format(node_id)
        return matches[0]

    def _get_cached_node(self, node_id):
        """Return node info from cache if possible, otherwise fetches it."""
        if node_id in self.cached_nodes:
            return self.cached_nodes[node_id]

        return self._get_node(node_id)

    def cleanup(self):
        self.tag_cache_update_event.set()
        self.tag_cache_kill_event.set()
