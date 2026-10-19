import pytest
from workload import run

SHARES = [0.1, 0.3, 0.5, 0.7, 0.9]


@pytest.mark.parametrize("case", range(300))
def test_work(request, case):
    run(request.node.nodeid, SHARES[case % len(SHARES)], units=100, depth=150)
