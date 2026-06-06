import topf


def test_import_smoke():
    assert hasattr(topf, "render")
    assert hasattr(topf, "scan")
