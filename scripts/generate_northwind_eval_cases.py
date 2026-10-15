"""Generate the versioned Northwind Baseline V1 evaluation fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evals" / "northwind_baseline_v1.json"
SMOKE_OUTPUT = ROOT / "evals" / "northwind_baseline_v1_smoke.json"
LOCK_OUTPUT = ROOT / "evals" / "northwind_baseline_v1.lock.json"
DATASET_COMMIT = "cd0ef28d66369fbe177778e604e4be0f153c9e5c"
DATASET_SHA256 = "0ee30c01ba282f7194f38bf7f99cd6be0470b7ee5f67d0f7ca41fb058d735e0c"
DATASET_RAW_URL = f"https://raw.githubusercontent.com/pthom/northwind_psql/{DATASET_COMMIT}/northwind.sql"
SMOKE_CASE_KEYS = [
    "NW001", "NW022", "NW033", "NW055", "NW065", "NW088",
    "NW095", "NW104", "NW115", "NW120", "NW136", "NW149",
]

CUSTOMER_COUNTRIES = [
    "Argentina", "Austria", "Belgium", "Brazil", "Canada", "Denmark",
    "Finland", "France", "Germany", "Ireland", "Italy", "Mexico",
    "Norway", "Poland", "Portugal", "Spain", "Sweden", "Switzerland",
    "UK", "USA", "Venezuela",
]

CATEGORIES = [
    "Beverages", "Condiments", "Confections", "Dairy Products",
    "Grains/Cereals", "Meat/Poultry", "Produce", "Seafood",
]

SUPPLIER_COUNTRIES = [
    "Australia", "Brazil", "Canada", "Denmark", "Finland", "France",
    "Germany", "Italy", "Japan", "Netherlands", "Norway", "Singapore",
    "Spain", "Sweden", "UK", "USA",
]

SHIPPERS = [
    "Alliance Shippers", "DHL", "Federal Shipping", "Speedy Express",
    "UPS", "United Package",
]

EMPLOYEES = [
    "Buchanan", "Callahan", "Davolio", "Dodsworth", "Fuller", "King",
    "Leverling", "Peacock", "Suyama",
]


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_cases() -> list[dict]:
    cases: list[dict] = []

    def add(
        category: str,
        difficulty: str,
        title: str,
        prompt: str,
        sql: str,
        *,
        report: bool = False,
        max_tool_calls: int = 4,
    ) -> None:
        number = len(cases) + 1
        required_tools = ["execute_sql", "create_report"] if report else ["execute_sql"]
        cases.append({
            "case_key": f"NW{number:03d}",
            "name": f"NW{number:03d} {title}",
            "category": category,
            "difficulty": difficulty,
            "prompt": prompt,
            "reference_sql": " ".join(sql.split()),
            "expected_tools": required_tools,
            "expect_report": report,
            "acceptance": {
                "data_match": "exact_rows_ignore_order",
                "required_tools": required_tools,
                "expect_report": report,
                "evidence_required": True,
                "max_tool_calls": max_tool_calls,
            },
        })

    # 1-32: simple filters and counts.
    for country in CUSTOMER_COUNTRIES:
        add(
            "simple_filter", "easy", f"Customers in {country}",
            f"Northwind 中有多少客户位于 {country}？只返回一列 customer_count。",
            f"SELECT COUNT(*)::int AS customer_count FROM customers WHERE country = {quote(country)}",
        )

    for category in CATEGORIES:
        add(
            "simple_filter", "easy", f"Products in {category}",
            f"统计 {category} 分类中的产品数量。只返回 category_name 和 product_count，并保留该分类名称。",
            f"""
            SELECT c.category_name, COUNT(p.product_id)::int AS product_count
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.category_id
            WHERE c.category_name = {quote(category)}
            GROUP BY c.category_name
            """,
        )

    for year in (1996, 1997, 1998):
        add(
            "simple_filter", "easy", f"Orders in {year}",
            f"统计 {year} 年下单的订单数量。只返回 order_year 和 order_count。",
            f"""
            SELECT {year}::int AS order_year, COUNT(*)::int AS order_count
            FROM orders
            WHERE order_date >= DATE '{year}-01-01' AND order_date < DATE '{year + 1}-01-01'
            """,
        )

    # 33-64: grouped aggregates.
    for country in CUSTOMER_COUNTRIES:
        add(
            "aggregation", "easy", f"Orders shipped to {country}",
            f"统计运往 {country} 的订单数量。只返回 ship_country 和 order_count。",
            f"""
            SELECT ship_country, COUNT(*)::int AS order_count
            FROM orders
            WHERE ship_country = {quote(country)}
            GROUP BY ship_country
            """,
        )

    for category in CATEGORIES:
        add(
            "aggregation", "medium", f"Revenue for {category}",
            f"计算 {category} 分类的折后净销售额。净销售额定义为 unit_price * quantity * (1 - discount)。只返回 category_name 和四舍五入到两位小数的 net_revenue。",
            f"""
            SELECT c.category_name,
                   ROUND(SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)), 2) AS net_revenue
            FROM categories c
            JOIN products p ON p.category_id = c.category_id
            JOIN order_details od ON od.product_id = p.product_id
            WHERE c.category_name = {quote(category)}
            GROUP BY c.category_name
            """,
        )

    for year in (1996, 1997, 1998):
        add(
            "aggregation", "easy", f"Freight in {year}",
            f"计算 {year} 年订单的总运费。只返回 order_year 和四舍五入到两位小数的 total_freight。",
            f"""
            SELECT {year}::int AS order_year, ROUND(SUM(freight)::numeric, 2) AS total_freight
            FROM orders
            WHERE order_date >= DATE '{year}-01-01' AND order_date < DATE '{year + 1}-01-01'
            """,
        )

    # 65-94: joins and ranking.
    for category in CATEGORIES:
        add(
            "join_ranking", "medium", f"Top products in {category}",
            f"列出 {category} 分类折后净销售额最高的 5 个产品。返回 product_name 和两位小数的 net_revenue，按 net_revenue 降序、product_name 升序。",
            f"""
            SELECT p.product_name,
                   ROUND(SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)), 2) AS net_revenue
            FROM products p
            JOIN categories c ON c.category_id = p.category_id
            JOIN order_details od ON od.product_id = p.product_id
            WHERE c.category_name = {quote(category)}
            GROUP BY p.product_id, p.product_name
            ORDER BY net_revenue DESC, p.product_name ASC
            LIMIT 5
            """,
            max_tool_calls=5,
        )

    for year in (1996, 1997, 1998):
        add(
            "join_ranking", "medium", f"Top customers in {year}",
            f"列出 {year} 年折后净销售额最高的 5 个客户。返回 company_name 和两位小数的 net_revenue，按 net_revenue 降序、company_name 升序。",
            f"""
            SELECT c.company_name,
                   ROUND(SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)), 2) AS net_revenue
            FROM customers c
            JOIN orders o ON o.customer_id = c.customer_id
            JOIN order_details od ON od.order_id = o.order_id
            WHERE o.order_date >= DATE '{year}-01-01' AND o.order_date < DATE '{year + 1}-01-01'
            GROUP BY c.customer_id, c.company_name
            ORDER BY net_revenue DESC, c.company_name ASC
            LIMIT 5
            """,
            max_tool_calls=5,
        )

    for year in (1996, 1997, 1998):
        add(
            "join_ranking", "medium", f"Top employees in {year}",
            f"列出 {year} 年负责订单的折后净销售额最高的 5 名员工。返回 employee_name 和两位小数的 net_revenue，按 net_revenue 降序、employee_name 升序。",
            f"""
            SELECT e.first_name || ' ' || e.last_name AS employee_name,
                   ROUND(SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)), 2) AS net_revenue
            FROM employees e
            JOIN orders o ON o.employee_id = e.employee_id
            JOIN order_details od ON od.order_id = o.order_id
            WHERE o.order_date >= DATE '{year}-01-01' AND o.order_date < DATE '{year + 1}-01-01'
            GROUP BY e.employee_id, e.first_name, e.last_name
            ORDER BY net_revenue DESC, employee_name ASC
            LIMIT 5
            """,
            max_tool_calls=5,
        )

    for country in SUPPLIER_COUNTRIES:
        add(
            "join_ranking", "medium", f"Supplier products in {country}",
            f"统计 {country} 供应商提供的产品数量。返回 supplier_country、supplier_count 和 product_count。",
            f"""
            SELECT s.country AS supplier_country,
                   COUNT(DISTINCT s.supplier_id)::int AS supplier_count,
                   COUNT(p.product_id)::int AS product_count
            FROM suppliers s
            LEFT JOIN products p ON p.supplier_id = s.supplier_id
            WHERE s.country = {quote(country)}
            GROUP BY s.country
            """,
        )

    # 95-119: time series and business metrics.
    for year in (1996, 1997, 1998):
        add(
            "time_series", "medium", f"Monthly revenue in {year}",
            f"按月统计 {year} 年折后净销售额。返回 month（YYYY-MM）和两位小数的 net_revenue，按 month 升序。",
            f"""
            SELECT TO_CHAR(o.order_date, 'YYYY-MM') AS month,
                   ROUND(SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)), 2) AS net_revenue
            FROM orders o
            JOIN order_details od ON od.order_id = o.order_id
            WHERE o.order_date >= DATE '{year}-01-01' AND o.order_date < DATE '{year + 1}-01-01'
            GROUP BY TO_CHAR(o.order_date, 'YYYY-MM')
            ORDER BY month
            """,
            max_tool_calls=5,
        )

    for year in (1996, 1997, 1998):
        add(
            "time_series", "easy", f"Monthly orders in {year}",
            f"按月统计 {year} 年订单量。返回 month（YYYY-MM）和 order_count，按 month 升序。",
            f"""
            SELECT TO_CHAR(order_date, 'YYYY-MM') AS month, COUNT(*)::int AS order_count
            FROM orders
            WHERE order_date >= DATE '{year}-01-01' AND order_date < DATE '{year + 1}-01-01'
            GROUP BY TO_CHAR(order_date, 'YYYY-MM')
            ORDER BY month
            """,
        )

    for year in (1996, 1997, 1998):
        add(
            "business_metric", "hard", f"Average order value in {year}",
            f"计算 {year} 年平均订单折后净销售额。先按订单汇总，再求平均。只返回 order_year 和两位小数的 avg_order_value。",
            f"""
            WITH order_values AS (
                SELECT o.order_id,
                       SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)) AS order_value
                FROM orders o
                JOIN order_details od ON od.order_id = o.order_id
                WHERE o.order_date >= DATE '{year}-01-01' AND o.order_date < DATE '{year + 1}-01-01'
                GROUP BY o.order_id
            )
            SELECT {year}::int AS order_year, ROUND(AVG(order_value), 2) AS avg_order_value
            FROM order_values
            """,
            max_tool_calls=5,
        )

    for shipper in SHIPPERS:
        add(
            "business_metric", "medium", f"Shipping delay for {shipper}",
            f"计算 {shipper} 已发货订单从下单到发货的平均天数。只返回 shipper_name 和两位小数的 avg_ship_days。",
            f"""
            SELECT s.company_name AS shipper_name,
                   ROUND(AVG(o.shipped_date - o.order_date)::numeric, 2) AS avg_ship_days
            FROM shippers s
            JOIN orders o ON o.ship_via = s.shipper_id
            WHERE s.company_name = {quote(shipper)} AND o.shipped_date IS NOT NULL
            GROUP BY s.company_name
            """,
        )

    for last_name in EMPLOYEES:
        add(
            "business_metric", "medium", f"Revenue for employee {last_name}",
            f"计算姓氏为 {last_name} 的员工负责订单的全部折后净销售额。返回 employee_name 和两位小数的 net_revenue。",
            f"""
            SELECT e.first_name || ' ' || e.last_name AS employee_name,
                   ROUND(SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)), 2) AS net_revenue
            FROM employees e
            JOIN orders o ON o.employee_id = e.employee_id
            JOIN order_details od ON od.order_id = o.order_id
            WHERE e.last_name = {quote(last_name)}
            GROUP BY e.employee_id, e.first_name, e.last_name
            """,
        )

    add(
        "business_metric", "medium", "Overall on-time shipping rate",
        "计算所有已发货订单中按时发货（shipped_date <= required_date）的比例。只返回 shipped_orders 和两位小数的 on_time_pct。",
        """
        SELECT COUNT(*)::int AS shipped_orders,
               ROUND(100.0 * COUNT(*) FILTER (WHERE shipped_date <= required_date) / NULLIF(COUNT(*), 0), 2) AS on_time_pct
        FROM orders
        WHERE shipped_date IS NOT NULL
        """,
    )

    # 120-135: data quality and edge cases.
    customer_fields = ["region", "postal_code", "phone", "fax", "contact_name"]
    for field in customer_fields:
        add(
            "data_quality", "easy", f"Null customer {field}",
            f"检查 customers.{field} 的空值数量。只返回 field_name 和 null_count。",
            f"SELECT {quote(field)} AS field_name, COUNT(*) FILTER (WHERE {field} IS NULL)::int AS null_count FROM customers",
        )

    order_fields = ["shipped_date", "ship_region", "ship_postal_code", "customer_id"]
    for field in order_fields:
        add(
            "data_quality", "easy", f"Null order {field}",
            f"检查 orders.{field} 的空值数量。只返回 field_name 和 null_count。",
            f"SELECT {quote(field)} AS field_name, COUNT(*) FILTER (WHERE {field} IS NULL)::int AS null_count FROM orders",
        )

    product_checks = [
        ("discontinued", "discontinued <> 0", "已停产产品"),
        ("zero_stock", "units_in_stock = 0", "零库存产品"),
        ("at_or_below_reorder", "units_in_stock <= reorder_level", "库存不高于补货点的产品"),
        ("units_on_order", "units_on_order > 0", "已有在途订货的产品"),
    ]
    for key, predicate, label in product_checks:
        add(
            "data_quality", "easy", f"Product check {key}",
            f"统计{label}数量。只返回 check_name 和 product_count。",
            f"SELECT {quote(key)} AS check_name, COUNT(*)::int AS product_count FROM products WHERE {predicate}",
        )

    for table in ("customer_demographics", "customer_customer_demo"):
        add(
            "data_quality", "easy", f"Empty table {table}",
            f"检查 {table} 表的行数。只返回 table_name 和 row_count，即使结果为 0 也要返回。",
            f"SELECT {quote(table)} AS table_name, COUNT(*)::int AS row_count FROM {table}",
        )

    add(
        "data_quality", "medium", "Duplicate customer companies",
        "查找 company_name 完全重复的客户公司。返回 company_name 和 duplicate_count；没有重复时返回空结果。",
        """
        SELECT company_name, COUNT(*)::int AS duplicate_count
        FROM customers
        GROUP BY company_name
        HAVING COUNT(*) > 1
        ORDER BY company_name
        """,
    )

    # 136-145: report generation.
    for category in CATEGORIES:
        add(
            "report", "hard", f"Category report for {category}",
            f"分析 {category} 分类的产品数量、当前库存数量和按当前单价计算的库存价值，并创建一份报告。报告必须包含文字结论。用于报告的最终查询只返回 category_name、product_count、units_in_stock 和两位小数的 inventory_value。",
            f"""
            SELECT c.category_name,
                   COUNT(p.product_id)::int AS product_count,
                   COALESCE(SUM(p.units_in_stock), 0)::int AS units_in_stock,
                   ROUND(COALESCE(SUM(p.unit_price::numeric * p.units_in_stock), 0), 2) AS inventory_value
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.category_id
            WHERE c.category_name = {quote(category)}
            GROUP BY c.category_name
            """,
            report=True,
            max_tool_calls=7,
        )

    for year in (1997, 1998):
        add(
            "report", "hard", f"Annual sales report {year}",
            f"生成 {year} 年销售概览报告，包含订单量、折后净销售额和平均运费，并给出文字结论。用于报告的最终查询只返回 order_year、order_count、两位小数的 net_revenue 和 avg_freight。",
            f"""
            WITH order_summary AS (
                SELECT COUNT(*)::int AS order_count,
                       ROUND(AVG(freight::numeric), 2) AS avg_freight
                FROM orders
                WHERE order_date >= DATE '{year}-01-01' AND order_date < DATE '{year + 1}-01-01'
            ), revenue_summary AS (
                SELECT ROUND(SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)), 2) AS net_revenue
                FROM orders o
                JOIN order_details od ON od.order_id = o.order_id
                WHERE o.order_date >= DATE '{year}-01-01' AND o.order_date < DATE '{year + 1}-01-01'
            )
            SELECT {year}::int AS order_year,
                   os.order_count,
                   rs.net_revenue,
                   os.avg_freight
            FROM order_summary os
            CROSS JOIN revenue_summary rs
            """,
            report=True,
            max_tool_calls=7,
        )

    # 146-150: advanced analytical cases.
    add(
        "advanced", "hard", "Customer order frequency distribution",
        "计算客户订单频次分布：先统计每个客户的订单数，再统计每种订单数对应的客户数量。返回 order_count 和 customer_count，按 order_count 升序。没有订单的客户也要计入 order_count=0。",
        """
        WITH customer_orders AS (
            SELECT c.customer_id, COUNT(o.order_id)::int AS order_count
            FROM customers c
            LEFT JOIN orders o ON o.customer_id = c.customer_id
            GROUP BY c.customer_id
        )
        SELECT order_count, COUNT(*)::int AS customer_count
        FROM customer_orders
        GROUP BY order_count
        ORDER BY order_count
        """,
        max_tool_calls=6,
    )

    add(
        "advanced", "hard", "Product reorder risk",
        "列出仍在售且现有库存加在途库存不高于补货点的产品。返回 product_name、units_in_stock、units_on_order 和 reorder_level，按 product_name 升序。",
        """
        SELECT product_name, units_in_stock, units_on_order, reorder_level
        FROM products
        WHERE discontinued = 0 AND units_in_stock + units_on_order <= reorder_level
        ORDER BY product_name
        """,
        max_tool_calls=5,
    )

    add(
        "advanced", "hard", "Late shipment rate by country",
        "按收货国家计算已发货订单的晚发货比例，晚发货定义为 shipped_date > required_date。返回 ship_country、shipped_orders 和两位小数的 late_pct，按 late_pct 降序、ship_country 升序。",
        """
        SELECT ship_country,
               COUNT(*)::int AS shipped_orders,
               ROUND(100.0 * COUNT(*) FILTER (WHERE shipped_date > required_date) / NULLIF(COUNT(*), 0), 2) AS late_pct
        FROM orders
        WHERE shipped_date IS NOT NULL
        GROUP BY ship_country
        ORDER BY late_pct DESC, ship_country ASC
        """,
        max_tool_calls=5,
    )

    add(
        "advanced", "hard", "Customer value ranking",
        "列出折后净销售额最高的 10 个客户，并同时给出最后下单日期和订单数。返回 company_name、last_order_date、order_count 和两位小数的 net_revenue，按 net_revenue 降序、company_name 升序。",
        """
        SELECT c.company_name,
               MAX(o.order_date) AS last_order_date,
               COUNT(DISTINCT o.order_id)::int AS order_count,
               ROUND(SUM(od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)), 2) AS net_revenue
        FROM customers c
        JOIN orders o ON o.customer_id = c.customer_id
        JOIN order_details od ON od.order_id = o.order_id
        GROUP BY c.customer_id, c.company_name
        ORDER BY net_revenue DESC, c.company_name ASC
        LIMIT 10
        """,
        max_tool_calls=6,
    )

    add(
        "advanced", "medium", "Employee reporting hierarchy",
        "统计每位经理的直接下属人数。返回 manager_name 和 direct_reports，按 direct_reports 降序、manager_name 升序。只返回至少有一名下属的经理。",
        """
        SELECT m.first_name || ' ' || m.last_name AS manager_name,
               COUNT(e.employee_id)::int AS direct_reports
        FROM employees m
        JOIN employees e ON e.reports_to = m.employee_id
        GROUP BY m.employee_id, m.first_name, m.last_name
        ORDER BY direct_reports DESC, manager_name ASC
        """,
        max_tool_calls=5,
    )

    assert len(cases) == 150, len(cases)
    assert len({case["name"] for case in cases}) == 150
    assert len({case["prompt"] for case in cases}) == 150
    return cases


def build_payload() -> dict:
    cases = build_cases()
    return {
        "schema_version": 1,
        "suite": {
            "name": "Northwind Baseline V1",
            "description": (
                "150 deterministic PostgreSQL cases covering filters, aggregates, joins, "
                "time series, data quality, reports, and advanced analysis."
            ),
            "target_type": "connection",
            "target_name": "Northwind Baseline",
            "dataset": {
                "source": "https://github.com/pthom/northwind_psql",
                "commit": DATASET_COMMIT,
                "sha256": DATASET_SHA256,
            },
        },
        "acceptance_defaults": {
            "data_match": "exact_rows_ignore_order",
            "evidence_required": True,
            "required_tool": "execute_sql",
        },
        "cases": cases,
    }


def canonical_json(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_smoke_payload(payload: dict) -> dict:
    smoke_keys = set(SMOKE_CASE_KEYS)
    smoke = json.loads(json.dumps(payload, ensure_ascii=False))
    smoke["suite"]["name"] = "Northwind Baseline V1 Smoke"
    smoke["suite"]["description"] = "12-case environment and tool-chain smoke subset of Northwind Baseline V1."
    smoke["cases"] = [case for case in smoke["cases"] if case["case_key"] in smoke_keys]
    return smoke


def build_lock(payload: dict, fixture_bytes: bytes, smoke_bytes: bytes) -> dict:
    cases = payload["cases"]
    return {
        "schema_version": 1,
        "fixture": OUTPUT.name,
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "smoke_fixture": SMOKE_OUTPUT.name,
        "smoke_fixture_sha256": hashlib.sha256(smoke_bytes).hexdigest(),
        "dataset_sha256": DATASET_SHA256,
        "dataset_raw_url": DATASET_RAW_URL,
        "case_count": len(cases),
        "category_counts": dict(sorted(Counter(case["category"] for case in cases).items())),
        "difficulty_counts": dict(sorted(Counter(case["difficulty"] for case in cases).items())),
        "smoke_case_keys": SMOKE_CASE_KEYS,
    }


def validate_payload(payload: dict) -> list[str]:
    errors: list[str] = []
    cases = payload.get("cases")
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(cases, list) or len(cases) != 150:
        return errors + ["cases must contain exactly 150 entries"]

    case_keys = [case.get("case_key") for case in cases]
    expected_keys = [f"NW{index:03d}" for index in range(1, 151)]
    if case_keys != expected_keys:
        errors.append("case_key values must be sequential from NW001 to NW150")
    for field in ("name", "prompt", "reference_sql"):
        values = [case.get(field) for case in cases]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            errors.append(f"every case must have a non-empty {field}")
        if len(set(values)) != len(values):
            errors.append(f"every case must have a unique {field}")

    for case in cases:
        key = case.get("case_key", "unknown")
        sql = str(case.get("reference_sql", "")).strip().upper()
        if not (sql.startswith("SELECT ") or sql.startswith("WITH ")):
            errors.append(f"{key}: reference_sql must be SELECT or WITH")
        acceptance = case.get("acceptance")
        if not isinstance(acceptance, dict):
            errors.append(f"{key}: acceptance must be an object")
            continue
        required_tools = acceptance.get("required_tools")
        if required_tools != case.get("expected_tools"):
            errors.append(f"{key}: required_tools must equal expected_tools")
        if acceptance.get("expect_report") != case.get("expect_report"):
            errors.append(f"{key}: expect_report is inconsistent")
        if acceptance.get("data_match") != "exact_rows_ignore_order":
            errors.append(f"{key}: unsupported data_match")
        if not isinstance(acceptance.get("max_tool_calls"), int) or acceptance["max_tool_calls"] < 1:
            errors.append(f"{key}: max_tool_calls must be a positive integer")

    missing_smoke = sorted(set(SMOKE_CASE_KEYS) - set(case_keys))
    if missing_smoke:
        errors.append(f"smoke cases are missing: {', '.join(missing_smoke)}")
    return errors


def _check_file(path: Path, expected: bytes) -> bool:
    if not path.exists():
        print(f"missing={path}", file=sys.stderr)
        return False
    if path.read_bytes() != expected:
        print(f"out_of_date={path}", file=sys.stderr)
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify committed artifacts without rewriting them.")
    parser.add_argument("--dataset", type=Path, help="Also verify a local northwind.sql file against the pinned SHA-256.")
    args = parser.parse_args()

    payload = build_payload()
    errors = validate_payload(payload)
    if errors:
        for error in errors:
            print(f"invalid={error}", file=sys.stderr)
        raise SystemExit(1)

    fixture_bytes = canonical_json(payload)
    smoke_bytes = canonical_json(build_smoke_payload(payload))
    lock = build_lock(payload, fixture_bytes, smoke_bytes)
    lock_bytes = canonical_json(lock)

    if args.check:
        ok = _check_file(OUTPUT, fixture_bytes) and _check_file(SMOKE_OUTPUT, smoke_bytes) and _check_file(LOCK_OUTPUT, lock_bytes)
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_bytes(fixture_bytes)
        SMOKE_OUTPUT.write_bytes(smoke_bytes)
        LOCK_OUTPUT.write_bytes(lock_bytes)
        ok = True

    if args.dataset:
        if not args.dataset.is_file():
            print(f"missing_dataset={args.dataset}", file=sys.stderr)
            ok = False
        else:
            actual_hash = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
            if actual_hash != DATASET_SHA256:
                print(f"dataset_sha256_mismatch={actual_hash}", file=sys.stderr)
                ok = False

    print(f"cases={len(payload['cases'])}")
    print(f"fixture_sha256={lock['fixture_sha256']}")
    print(f"mode={'check' if args.check else 'generate'}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
