SKILLS = {
    "excel-handling": "Before analysing XLSX, inspect sheets and header rows. Explain merged cells, formula cache limits, blank rows, dates, and totals rows. Prefer data profiling before aggregation.",
    "data-profiling": "Profile data before conclusions: row count, missing values, duplicates, distributions, outliers, field types, and suspicious values. Separate evidence from inference.",
    "postgres-analysis": "Use only inspected schema and quoted identifiers when necessary. Start with narrow SELECTs, explicit joins, safe time filters, and read-only SQL.",
    "reporting": "State the question, evidence, SQL/query ids, key findings, caveats, and recommended next actions. Use charts only when they clarify a comparison or trend.",
}


def load_skill(name: str) -> str | None:
    return SKILLS.get(name)
