import pytest

from ray.autoscaler.aws.config import _resource_cache
from ray.autoscaler.aws.node_provider import AWSNodeProvider

from botocore.stub import Stubber


@pytest.fixture()
def iam_client_stub():
    resource = _resource_cache("iam", "us-west-2")
    with Stubber(resource.meta.client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


@pytest.fixture()
def ec2_client_stub():
    resource = _resource_cache("ec2", "us-west-2")
    with Stubber(resource.meta.client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


@pytest.fixture(scope="module")
def noop_node_provider():
    node_provider = AWSNodeProvider({"region": "us-west-2"}, "test-cluster")
    node_provider.cleanup()  # We don't actually need this running

    yield node_provider
