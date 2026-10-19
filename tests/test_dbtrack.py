from flakytest.profiling.dbtrack import QueryStats, normalize_sql


def test_normalize_sql_replaces_literals_and_in_lists():
    sql = "SELECT * FROM t WHERE id = 12 AND name = 'bob''s'  AND x IN (%s, %s, %s)"
    assert normalize_sql(sql) == "SELECT * FROM t WHERE id = ? AND name = ? AND x IN (…)"


def test_query_stats_reports_repeated_queries():
    stats = QueryStats()
    for sql in ["SELECT * FROM orders_item WHERE order_id = 1"] * 4 + ["SELECT * FROM orders_item WHERE order_id = 2"]:
        stats.add(sql, 0.01)
    for _ in range(6):
        stats.add("SAVEPOINT s1", 0.001)
        stats.add("BEGIN", 0.001, "sqlalchemy")
    result = stats.as_dict()
    assert result["queries"] == 17
    assert result["duplicates"] == [
        {"sql": "SELECT * FROM orders_item WHERE order_id = ?", "count": 5, "source": "django"}
    ]


def test_statements_that_differ_only_in_literals_are_counted_together():
    stats = QueryStats()
    for size in range(1, 5):
        stats.add(f"SELECT * FROM t WHERE id IN ({', '.join(['%s'] * size)})", 0.001)  # noqa: S608
    stats.add("  release savepoint s1", 0.001)
    assert stats.statements[("django", "  release savepoint s1")] == 0  # control statements aren't kept
    assert stats.as_dict()["duplicates"] == [{"sql": "SELECT * FROM t WHERE id IN (…)", "count": 4, "source": "django"}]
    assert "commits" not in stats.as_dict()  # only reported where commits are measured (Django)
