from patch_verifier.patches import is_binary_patch


def test_binary_marker_is_only_metadata_on_an_unprefixed_line() -> None:
    textual_patch = (
        b"diff --git a/value.py b/value.py\n"
        b"--- a/value.py\n"
        b"+++ b/value.py\n"
        b"@@ -1 +1 @@\n"
        b"-message = 'old'\n"
        b"+message = 'GIT binary patch'\n"
    )

    assert not is_binary_patch(textual_patch)
    assert is_binary_patch(b"GIT binary patch\nliteral 1\nAcmZQz\n")
    assert is_binary_patch(b"GIT binary patch\r\nliteral 1\r\n")
    assert is_binary_patch(b"diff --git a/a b/a\n\0")
