from operator import itemgetter

from hypothesis import given, strategies as st
import pytest

from ray.autoscaler.tags import TAG_RAY_CLUSTER_NAME


class HypothesisStrategies:
    KEYS: st.SearchStrategy = st.text(min_size=1)
    VALUES: st.SearchStrategy = st.text()

    RAW_TAGS: st.SearchStrategy = st.dictionaries(
        KEYS,
        VALUES,
    )

    TAG_SPEC: st.SearchStrategy = st.lists(
        st.fixed_dictionaries({
            "ResourceType": st.text(min_size=1),
            "Tags": st.lists(
                st.fixed_dictionaries({
                    "Key": KEYS,
                    "Value": VALUES,
                }),
                unique_by=itemgetter("Key"),
            ),
        }),
        unique_by=itemgetter("ResourceType"))

    NODE_CONFIG: st.SearchStrategy = st.fixed_dictionaries({
        "TagSpecifications": TAG_SPEC,
    })


class TestMergingTagSpecs:
    @given(
        left=HypothesisStrategies.TAG_SPEC,
        right=HypothesisStrategies.TAG_SPEC,
    )
    def test_merge(self, noop_node_provider, left, right):  # type: ignore
        merged = noop_node_provider._merge_tag_specs(left, right)

        for test in (
                self.no_missing_resources,
                self.right_fully_set,
                self.left_tags_set,
        ):
            test(left, right, merged)

    @staticmethod
    def no_missing_resources(left, right, merged):
        # Extract the ResourceTypes from both left, right, and merged
        left_types = set(s["ResourceType"] for s in left)
        right_types = set(s["ResourceType"] for s in right)
        merged_types = set(s["ResourceType"] for s in merged)

        assert merged_types == (left_types | right_types), \
            "Missing resource_types: {}".format(
                (left_types | right_types) - merged_types,
            )

    @staticmethod
    def right_fully_set(left, right, merged):
        """
        Make sure all of the tags in `right` are set in `merged` to make sure
        any conflicts between `left` & `right` won by `right`.
        """
        for right_spec in right:
            matching_spec = next(
                spec for spec in merged
                if spec["ResourceType"] == right_spec["ResourceType"])

            right_tags = frozenset(
                frozenset(tag.items()) for tag in right_spec["Tags"])
            matching_tags = frozenset(
                frozenset(tag.items()) for tag in matching_spec["Tags"])

            assert matching_tags >= right_tags, \
                "Right side tags are missing: {}".format(
                    right_tags - matching_tags,
                )

    @staticmethod
    def left_tags_set(left, right, merged):
        """
        Any keys for tags in left, should be in merged (the values may be
        overwritten so we don't check them)
        """
        for left_spec in left:
            matching_spec = next(
                spec for spec in merged
                if spec["ResourceType"] == left_spec["ResourceType"])

            left_tags = set(tag["Key"] for tag in left_spec["Tags"])
            matching_tags = set(tag["Key"] for tag in matching_spec["Tags"])

            assert matching_tags >= left_tags, \
                "Left side tag keys are missing: {}".format(
                    left_tags - matching_tags,
                )


@given(
    node_config=HypothesisStrategies.NODE_CONFIG,
    tags=HypothesisStrategies.RAW_TAGS,
)
def test_parity_with_old(noop_node_provider, node_config, tags):
    # Copied from the old implementation
    def old_impl(self, conf, tags):  # type: ignore
        config_provided_tags = [{
            "Key": k,
            "Value": v
        } for k, v in conf.get('tags', {}).items()]

        tag_pairs = config_provided_tags + [{
            "Key": TAG_RAY_CLUSTER_NAME,
            "Value": self.cluster_name,
        }]
        for k, v in tags.items():
            tag_pairs.append({
                "Key": k,
                "Value": v,
            })
        tag_specs = [{
            "ResourceType": "instance",
            "Tags": tag_pairs,
        }]
        user_tag_specs = conf.get("TagSpecifications", [])
        # Allow users to add tags and override values of existing
        # tags with their own. This only applies to the resource type
        # "instance". All other resource types are appended to the list of
        # tag specs.
        for user_tag_spec in user_tag_specs:
            if user_tag_spec["ResourceType"] == "instance":
                for user_tag in user_tag_spec["Tags"]:
                    exists = False
                    for tag in tag_specs[0]["Tags"]:
                        if user_tag["Key"] == tag["Key"]:
                            exists = True
                            tag["Value"] = user_tag["Value"]
                            break
                    if not exists:
                        tag_specs[0]["Tags"] += [user_tag]
            else:
                tag_specs += [user_tag_spec]

        return tag_specs

    # I don't want to trust the old implementation not somehow be mutating
    # state
    old = old_impl(noop_node_provider, node_config.copy(), tags.copy())
    new = noop_node_provider._generate_tag_specs(node_config, tags)

    # Before we compare old & new, we want to run old through our new
    # canonicalize function. We want to do this because we don't want
    # ordering differences to cause failures since they don't mater to AWS.
    old = noop_node_provider._canonical_tag_spec(old)

    assert old == new


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main(["-v", __file__]))
