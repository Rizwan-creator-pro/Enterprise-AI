"""
Enterprise AI Hub - In-Memory Safe Query Engine
================================================
Translates a high-level JSON plan produced by the LLM planner into safe, exact
Python aggregations over the user's live authorized dataset.

Features:
  - Strict sandbox: never uses eval(), exec(), or arbitrary code.
  - Operations supported:
      * filter: ==, !=, >, >=, <, <=, in, not in, contains, startswith, is_null, not_null, between
      * group_by: 1 to 3 columns
      * aggregates: count, count_distinct, sum, avg, min, max
      * sort: asc / desc, by any column or aggregate
      * limit / top-N / bottom-N
      * pivot: row_dim, col_dim, val_dim, agg (e.g. city x department x avg(salary))
      * compute KPIs: total_rows, overall sums, averages, max/min
  - Automatic type coercion (handles "$120,000", "75,000.50", "2026-01-05T00:00:00Z").
  - Produces both:
      1. Clean Markdown table with exact numbers
      2. Apache ECharts JSON config (exact numbers, properly formatted)
      3. KPI dictionary for the UI cards
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# Value Parsing & Cleaning
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"^[+-]?[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?$|^[+-]?[0-9]+(?:\.[0-9]+)?$")
_CURR_RE = re.compile(r"^[^\w\s]*\s*([+-]?[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[+-]?[0-9]+(?:\.[0-9]+)?)\s*[^\w\s]*$")


def parse_number(val: Any) -> Optional[float]:
    """Extract a float from numeric strings, currency symbols, commas. Returns None if not numeric."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    # Quick strip common currency prefixes/suffixes: PKR, Rs, $, EUR, commas
    cleaned = re.sub(r"^(?:pkr|rs\.?|usd|\$|€|£)\s*", "", s, flags=re.I)
    cleaned = re.sub(r"\s*(?:pkr|rs\.?|usd|\$|€|£)$", "", cleaned, flags=re.I).strip()
    cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%m-%Y",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(s.split(".")[0].rstrip("Z"), fmt.rstrip("Z"))
        except ValueError:
            pass
    return None


def format_number(val: Any) -> str:
    """Friendly formatting for display: integers comma-separated, floats rounded to 2 decimals."""
    if val is None:
        return ""
    if isinstance(val, float):
        if val.is_integer():
            return f"{int(val):,}"
        return f"{val:,.2f}"
    if isinstance(val, int):
        return f"{val:,}"
    return str(val)


# ---------------------------------------------------------------------------
# Filter Sandbox
# ---------------------------------------------------------------------------

def _compare(cell: Any, op: str, target: Any) -> bool:
    op = (op or "==").lower().strip()
    # Null checks
    if op in ("is_null", "is null", "null"):
        return cell is None or str(cell).strip() == ""
    if op in ("not_null", "not null", "not_null", "present"):
        return cell is not None and str(cell).strip() != ""

    if cell is None:
        return False

    # Try numeric compare first
    c_num = parse_number(cell)
    t_num = parse_number(target) if not isinstance(target, list) else None

    if c_num is not None and t_num is not None and op in ("==", "!=", ">", ">=", "<", "<="):
        if op == "==": return abs(c_num - t_num) < 1e-9
        if op == "!=": return abs(c_num - t_num) >= 1e-9
        if op == ">":  return c_num > t_num
        if op == ">=": return c_num >= t_num
        if op == "<":  return c_num < t_num
        if op == "<=": return c_num <= t_num

    # Date compare
    c_dt = parse_date(cell)
    t_dt = parse_date(target) if not isinstance(target, list) else None
    if c_dt is not None and t_dt is not None and op in ("==", "!=", ">", ">=", "<", "<="):
        if op == "==": return c_dt == t_dt
        if op == "!=": return c_dt != t_dt
        if op == ">":  return c_dt > t_dt
        if op == ">=": return c_dt >= t_dt
        if op == "<":  return c_dt < t_dt
        if op == "<=": return c_dt <= t_dt

    # Between
    if op == "between" and isinstance(target, (list, tuple)) and len(target) == 2:
        lo, hi = parse_number(target[0]), parse_number(target[1])
        if c_num is not None and lo is not None and hi is not None:
            return lo <= c_num <= hi
        return str(target[0]).lower() <= str(cell).lower() <= str(target[1]).lower()

    # In / Not in
    if op in ("in", "not in"):
        targets = target if isinstance(target, list) else [target]
        targets_lower = {str(x).strip().lower() for x in targets}
        c_str = str(cell).strip().lower()
        # also match numeric equivalence
        matched = c_str in targets_lower
        if not matched and c_num is not None:
            t_nums = {parse_number(x) for x in targets}
            matched = any(tn is not None and abs(c_num - tn) < 1e-9 for tn in t_nums)
        return matched if op == "in" else not matched

    # String operations (case-insensitive)
    s_cell = str(cell).strip().lower()
    s_target = str(target).strip().lower()
    if op in ("==", "eq", "equals"):      return s_cell == s_target
    if op in ("!=", "neq", "not_equals"): return s_cell != s_target
    if op in ("contains", "like"):        return s_target in s_cell
    if op in ("startswith", "starts_with"): return s_cell.startswith(s_target)
    if op in ("endswith", "ends_with"):     return s_cell.endswith(s_target)

    return False


def apply_filters(rows: List[dict], filters: List[dict]) -> List[dict]:
    """filters: [{"col": "status", "op": "==", "val": "Active"}, ...]"""
    if not filters:
        return rows
    out = []
    for r in rows:
        match = True
        for f in filters:
            col = f.get("col") or f.get("field")
            op = f.get("op", "==")
            val = f.get("val") if "val" in f else f.get("value")
            # Case-insensitive column lookup
            val_in_row = None
            for k in r:
                if k.lower() == str(col).lower():
                    val_in_row = r[k]
                    break
            if not _compare(val_in_row, op, val):
                match = False
                break
        if match:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Aggregation Engine
# ---------------------------------------------------------------------------

def _run_agg(values: List[Any], agg: str) -> Optional[float]:
    agg = (agg or "count").lower().strip()
    if agg == "count":
        return len(values)
    if agg == "count_distinct":
        return len({str(v) for v in values if v is not None and str(v).strip() != ""})

    nums = [parse_number(v) for v in values]
    nums = [n for n in nums if n is not None]
    if not nums:
        return 0 if agg in ("sum", "count") else None

    if agg == "sum":   return round(sum(nums), 4)
    if agg in ("avg", "average", "mean"): return round(sum(nums) / len(nums), 4)
    if agg == "min":   return min(nums)
    if agg == "max":   return max(nums)
    return None


def aggregate(
    rows: List[dict],
    group_by: List[str],
    aggs: List[dict],
) -> List[dict]:
    """
    group_by: ["department", "city"]
    aggs: [
      {"col": "*", "agg": "count", "as": "Employee Count"},
      {"col": "salary", "agg": "avg", "as": "Average Salary"},
      {"col": "salary", "agg": "sum", "as": "Total Salary"},
    ]
    """
    if not rows:
        return []

    # Map actual column names case-insensitively
    first = rows[0]
    actual_cols = {c.lower(): c for c in first.keys()}

    resolved_gb = [actual_cols.get(g.lower(), g) for g in group_by]

    # Group rows
    groups: Dict[Tuple, List[dict]] = {}
    for r in rows:
        key = tuple(r.get(c) for c in resolved_gb)
        groups.setdefault(key, []).append(r)

    # Compute aggregates for each group
    out = []
    for key, g_rows in groups.items():
        row_dict = {}
        for c, k_val in zip(resolved_gb, key):
            row_dict[c] = k_val

        for a in aggs:
            col_name = a.get("col", "*")
            agg_type = a.get("agg", "count")
            alias = a.get("as") or f"{agg_type.upper()}({col_name})"
            if col_name == "*":
                vals = [1] * len(g_rows)
            else:
                real_col = actual_cols.get(str(col_name).lower(), col_name)
                vals = [r.get(real_col) for r in g_rows]
            row_dict[alias] = _run_agg(vals, agg_type)

        out.append(row_dict)

    return out


# ---------------------------------------------------------------------------
# Pivot (for 2-dimensional charts & matrix tables)
# ---------------------------------------------------------------------------

def pivot(
    rows: List[dict],
    row_dim: str,
    col_dim: str,
    val_dim: str,
    agg: str = "sum",
) -> Tuple[List[str], List[dict]]:
    """
    Creates a 2D cross-tab:
    e.g. row_dim='city', col_dim='department', val_dim='salary', agg='avg'
    Returns: (columns_list, result_rows)
    """
    if not rows:
        return [], []

    first = rows[0]
    actual_cols = {c.lower(): c for c in first.keys()}
    r_col = actual_cols.get(row_dim.lower(), row_dim)
    c_col = actual_cols.get(col_dim.lower(), col_dim)
    v_col = actual_cols.get(val_dim.lower(), val_dim)

    # Collect distinct column values
    col_values = sorted({str(r.get(c_col) or "Unknown") for r in rows})

    # Group by (r_val, c_val)
    cells: Dict[Tuple[Any, str], List[Any]] = {}
    row_keys = []
    seen_row_keys = set()
    for r in rows:
        rk = r.get(r_col)
        if rk not in seen_row_keys:
            seen_row_keys.add(rk)
            row_keys.append(rk)
        ck = str(r.get(c_col) or "Unknown")
        cells.setdefault((rk, ck), []).append(r.get(v_col))

    result_rows = []
    for rk in row_keys:
        row = {r_col: rk}
        for ck in col_values:
            vals = cells.get((rk, ck), [])
            row[ck] = _run_agg(vals, agg) if vals else 0
        result_rows.append(row)

    headers = [r_col] + col_values
    return headers, result_rows


# ---------------------------------------------------------------------------
# Sorting & Limit
# ---------------------------------------------------------------------------

def sort_rows(rows: List[dict], order_by: List[dict]) -> List[dict]:
    """order_by: [{"col": "Total Salary", "desc": True}]"""
    if not order_by or not rows:
        return rows

    def sort_key(row):
        keys = []
        for o in order_by:
            col = o.get("col")
            desc = o.get("desc", False)
            v = None
            for k in row:
                if k.lower() == str(col).lower():
                    v = row[k]
                    break
            num = parse_number(v)
            if num is not None:
                # Use number
                val = (-num if desc else num)
            elif v is None:
                val = ""
            else:
                val = str(v).lower()
                if desc:
                    # Negate string sort by wrapping in custom reverse wrapper
                    val = _ReverseString(val)
            keys.append(val)
        return tuple(keys)

    class _ReverseString:
        def __init__(self, s): self.s = s
        def __lt__(self, o): return self.s > o.s
        def __gt__(self, o): return self.s < o.s
        def __eq__(self, o): return self.s == o.s

    return sorted(rows, key=sort_key)


# ---------------------------------------------------------------------------
# KPI Summary Calculator
# ---------------------------------------------------------------------------

def compute_kpis(rows: List[dict], num_cols: Optional[List[str]] = None) -> List[dict]:
    """
    Computes 2 to 4 key cards from the dataset:
    [{"label": "Total Employees", "value": "150", "sub": "across 4 cities"}, ...]
    """
    if not rows:
        return []

    first = rows[0]
    cards = [{"label": "Total Records", "value": f"{len(rows):,}", "sub": "rows analyzed"}]

    # Detect numeric columns if not supplied
    if not num_cols:
        candidate_cols = []
        for k in first:
            lk = str(k).lower().strip()
            # Exclude IDs, codes, links, and contact numbers from numeric KPIs
            if lk == "id" or lk.endswith("_id") or lk.startswith("id_") or lk in (
                "emp_id", "user_id", "dept_id", "deptid", "product_id", "access_id",
                "links", "phone", "contact", "code", "zip", "postal", "extension"
            ):
                continue
            vals = [parse_number(r.get(k)) for r in rows[:20]]
            if len([v for v in vals if v is not None]) >= 10:
                candidate_cols.append(k)
        num_cols = candidate_cols

    for col in num_cols[:2]:
        nums = [parse_number(r.get(col)) for r in rows]
        nums = [n for n in nums if n is not None]
        if not nums:
            continue
        total = sum(nums)
        avg = total / len(nums)
        col_title = col.replace("_", " ").title()
        if total > 1000 or "salary" in col.lower() or "price" in col.lower() or "amount" in col.lower():
            cards.append({
                "label": f"Total {col_title}",
                "value": format_number(round(total, 2)),
                "sub": f"Avg: {format_number(round(avg, 2))}"
            })
        else:
            cards.append({
                "label": f"Avg {col_title}",
                "value": format_number(round(avg, 2)),
                "sub": f"Min: {format_number(min(nums))} · Max: {format_number(max(nums))}"
            })

    return cards[:4]


# ---------------------------------------------------------------------------
# ECharts Generation
# ---------------------------------------------------------------------------

def build_echarts(
    chart_type: str,
    title: str,
    data_rows: List[dict],
    cat_col: str,
    val_cols: List[str],
    subtext: str = "",
) -> Optional[dict]:
    """
    chart_type: 'bar', 'donut', 'line', 'rose', 'stacked_bar'
    """
    if not data_rows or not cat_col or not val_cols:
        return None

    # Auto-prefer descriptive names like 'department' over raw numeric IDs like 'dept_id'
    if str(cat_col).lower() in ("dept_id", "deptid") and any(k.lower() in ("department", "dept_name", "department_name") for k in data_rows[0]):
        for k in data_rows[0]:
            if k.lower() in ("department", "dept_name", "department_name"):
                cat_col = k
                break

    cat_col_actual = None
    for k in data_rows[0]:
        if k.lower() == cat_col.lower():
            cat_col_actual = k
            break
    if not cat_col_actual:
        cat_col_actual = cat_col

    chart_type = (chart_type or "bar").lower().strip()

    # Donut / Rose (single value column)
    if chart_type in ("donut", "pie", "rose"):
        v_col = val_cols[0]
        points = []
        for r in data_rows[:15]:
            v = parse_number(r.get(v_col))
            if v is not None:
                points.append({"name": str(r.get(cat_col_actual) or "Unknown"), "value": round(v, 2)})
        series = {"type": "pie", "data": points}
        if chart_type == "rose":
            series["roseType"] = "area"
        return {
            "title": {"text": title, "subtext": subtext or f"by {cat_col_actual.replace('_', ' ')}"},
            "series": [series]
        }

    # Bar / Line / Grouped / Stacked
    categories = [str(r.get(cat_col_actual) or "") for r in data_rows[:25]]
    series_list = []
    for vc in val_cols:
        v_actual = None
        for k in data_rows[0]:
            if k.lower() == vc.lower():
                v_actual = k
                break
        v_actual = v_actual or vc

        values = []
        for r in data_rows[:25]:
            val = parse_number(r.get(v_actual))
            values.append(round(val, 2) if val is not None else 0)

        s_obj = {
            "name": v_actual.replace("_", " ").title(),
            "type": "line" if chart_type == "line" else "bar",
            "data": values
        }
        if chart_type == "stacked_bar":
            s_obj["stack"] = "total"
        series_list.append(s_obj)

    # If exactly 2 series have very different scales (e.g. employee count vs total salary),
    # use dual y-axes so neither series is crushed flat in the chart!
    if len(series_list) == 2 and chart_type != "stacked_bar":
        max0 = max([abs(v) for v in series_list[0]["data"]] or [1])
        max1 = max([abs(v) for v in series_list[1]["data"]] or [1])
        ratio = max(max0, max1) / max(min(max0, max1), 0.001)
        if ratio > 2.5:
            series_list[0]["yAxisIndex"] = 0
            series_list[1]["yAxisIndex"] = 1
            return {
                "title": {"text": title, "subtext": subtext},
                "tooltip": {"trigger": "axis"},
                "legend": {"data": [s["name"] for s in series_list]},
                "xAxis": {"type": "category", "data": categories, "name": cat_col_actual.replace("_", " ").title()},
                "yAxis": [
                    {"type": "value", "name": series_list[0]["name"]},
                    {"type": "value", "name": series_list[1]["name"], "splitLine": {"show": False}}
                ],
                "series": series_list
            }

    return {
        "title": {"text": title, "subtext": subtext},
        "xAxis": {"type": "category", "data": categories, "name": cat_col_actual.replace("_", " ").title()},
        "yAxis": {"type": "value"},
        "series": series_list
    }


# ---------------------------------------------------------------------------
# Markdown Table Formatter
# ---------------------------------------------------------------------------

def rows_to_md_table(rows: List[dict], max_rows: int = 250) -> str:
    if not rows:
        return "_No matching records found._"
    cols = [c for c in rows[0].keys() if c != "links"]
    head = "| " + " | ".join(c.replace("_", " ").title() for c in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    lines = [head, sep]
    for r in rows[:max_rows]:
        line = "| " + " | ".join(format_number(r.get(c)) for c in cols) + " |"
        lines.append(line)
    if len(rows) > max_rows:
        lines.append(f"\n_Showing first {max_rows} of {len(rows)} rows._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main Plan Execution Dispatcher
# ---------------------------------------------------------------------------

def execute_plan(dataset: List[dict], plan: dict) -> dict:
    """
    Executes a structured JSON plan produced by the planner LLM.
    Returns:
      {
        "table_rows": List[dict],
        "table_md": str,
        "kpis": List[dict],
        "chart": Optional[dict],
        "row_count": int,
        "mode": "raw" | "aggregate" | "pivot"
      }
    """
    rows = dataset
    # Step 1: Filter
    filters = plan.get("filters") or []
    if filters:
        rows = apply_filters(rows, filters)

    mode = plan.get("mode", "aggregate" if plan.get("group_by") else "raw")

    # Step 2: Aggregate / Pivot / Select
    chart_obj = None
    if mode == "pivot" and plan.get("pivot"):
        p = plan["pivot"]
        headers, table_rows = pivot(
            rows,
            p.get("row_dim"),
            p.get("col_dim"),
            p.get("val_dim"),
            p.get("agg", "sum")
        )
    elif mode == "aggregate" and plan.get("group_by"):
        # Auto-prefer descriptive column 'department' over 'dept_id' if available
        gb = []
        for g in plan["group_by"]:
            if str(g).lower() in ("dept_id", "deptid") and rows and any(k.lower() in ("department", "dept_name") for k in rows[0]):
                dept_col = next(k for k in rows[0] if k.lower() in ("department", "dept_name"))
                gb.append(dept_col)
            else:
                gb.append(g)
        table_rows = aggregate(
            rows,
            gb,
            plan.get("aggs") or [{"col": "*", "agg": "count", "as": "Count"}]
        )
    else:
        # Raw rows: select columns if specified
        cols = plan.get("select_cols")
        if cols and rows:
            actual = {c.lower(): c for c in rows[0].keys()}
            selected = [actual.get(c.lower(), c) for c in cols if c.lower() in actual]
            table_rows = [{c: r.get(c) for c in selected} for r in rows]
        else:
            table_rows = rows

    # Step 3: Sort
    if plan.get("order_by"):
        table_rows = sort_rows(table_rows, plan["order_by"])

    # Step 4: Limit
    if plan.get("limit"):
        table_rows = table_rows[:int(plan["limit"])]

    # Step 5: Chart
    c = plan.get("chart")
    if c and table_rows:
        chart_obj = build_echarts(
            chart_type=c.get("type", "bar"),
            title=c.get("title") or "Chart",
            data_rows=table_rows,
            cat_col=c.get("cat_col") or (plan.get("group_by", [None])[0]),
            val_cols=c.get("val_cols") or [k for k in table_rows[0].keys() if parse_number(table_rows[0][k]) is not None][:1],
            subtext=c.get("subtext", "")
        )

    # Step 6: KPIs
    kpis = compute_kpis(rows if mode == "raw" else table_rows)

    return {
        "table_rows": table_rows,
        "table_md": rows_to_md_table(table_rows),
        "kpis": kpis,
        "chart": chart_obj,
        "row_count": len(table_rows),
        "source_rows": len(rows),
        "mode": mode,
    }
