import random


def test_always_pass():
    assert True


def test_always_fail():
    assert False


def test_flaky():
    assert random.randint(0, 1) < 0.5
