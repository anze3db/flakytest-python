import textwrap

import pytest

from flakytest.profiling.duplicates import mark_same_code, plain


def copies(tmp_path, files):
    """The same_code duplicates mark_same_code finds in these test files: {copy: original}."""
    tests = []
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("class "):
                cls = stripped[6:].split("(")[0].split(":")[0]
            if stripped.startswith("def test"):
                function = stripped[4:].split("(")[0]
                owner = cls if line.startswith("    ") else None
                nodeid = f"{name}::{owner}::{function}" if owner else f"{name}::{function}"
                tests.append({"nodeid": nodeid, "file": name, "class": owner, "name": function})
    mark_same_code(tests, tmp_path)
    return {t["nodeid"]: t["duplicate"]["of"] for t in tests if t.get("duplicate")}


def test_a_copy_with_other_variable_names_and_docstring(tmp_path):
    found = copies(
        tmp_path,
        {
            "tests/test_copy.py": """
            def test_dunder_copy(model):
                copied = model.__copy__()
                assert copied == model

            def test_model_copy(model):
                \"\"\"model_copy() copies the model.\"\"\"
                other = model.__copy__()  # pasted from above
                assert other == model
            """
        },
    )
    assert found == {"tests/test_copy.py::test_model_copy": "tests/test_copy.py::test_dunder_copy"}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # another constant: another scenario
        ("def test_a(model):\n    assert model.copy(1)", "def test_b(model):\n    assert model.copy(2)"),
        # other parametrize data
        (
            "@pytest.mark.parametrize('x', [1])\ndef test_a(model, x):\n    assert model.copy(x)",
            "@pytest.mark.parametrize('x', [2])\ndef test_b(model, x):\n    assert model.copy(x)",
        ),
        # another fixture
        ("def test_a(model):\n    assert model.copy()", "def test_b(other):\n    assert other.copy()"),
        # placeholders all look the same
        ("def test_a():\n    pass", "def test_b():\n    pass"),
    ],
)
def test_tests_that_differ_are_not_copies(tmp_path, first, second):
    assert copies(tmp_path, {"tests/test_copy.py": f"import pytest\n\n{first}\n\n{second}\n"}) == {}


def test_methods_are_copies_only_in_the_same_class(tmp_path):
    found = copies(
        tmp_path,
        {
            "tests/test_views.py": """
            from django.test import TestCase, override_settings

            class LogoutTest(TestCase):
                def test_logout_default(self):
                    response = self.client.post("/logout/")
                    self.assertEqual(response.status_code, 302)

                def test_logout_with_post(self):
                    response = self.client.post("/logout/")
                    self.assertEqual(response.status_code, 302)

            @override_settings(LOGOUT_REDIRECT_URL="/")
            class OtherLogoutTest(TestCase):
                def test_logout_default(self):
                    response = self.client.post("/logout/")
                    self.assertEqual(response.status_code, 302)
            """
        },
    )
    assert found == {
        "tests/test_views.py::LogoutTest::test_logout_with_post": "tests/test_views.py::LogoutTest::test_logout_default"
    }


def test_names_must_mean_the_same_in_both_modules(tmp_path):
    test = "def test_value():\n    assert copy_model(1) == 1\n"
    found = copies(
        tmp_path,
        {
            "tests/test_a.py": "from shop import copy_model\n\n" + test,
            "tests/test_b.py": "from shop import copy_model\n\n" + test,
            "tests/test_c.py": "def copy_model(x):\n    return x\n\n" + test,  # its own copy_model
            "other/test_d.py": "from shop import copy_model\n\n" + test,  # another conftest.py
        },
    )
    assert found == {"tests/test_b.py::test_value": "tests/test_a.py::test_value"}


def test_plain_values():
    assert plain(("eur", 1, None, [1.5, {"a": b"x"}]))
    assert not plain(object())
    assert not plain([1, object()])
