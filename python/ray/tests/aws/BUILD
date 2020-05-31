load("@rules_python//python:defs.bzl", "py_test")

py_test(
    name = "test_autoscaler_aws",
    size = "small",
    srcs = ["test_autoscaler_aws.py"],
    deps = ["//:ray_lib"],
)

py_test(
    name = "test_autoscaler_tags",
    size = "small",
    srcs = ["test_autoscaler_tags.py"],
    deps = ["//:ray_lib"],
)
