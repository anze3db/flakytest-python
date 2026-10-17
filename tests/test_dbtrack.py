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
