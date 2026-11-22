"""Сборка SQL из выбранных полей, операций и значений."""

from psycopg import sql


COMPARISONS = ("=", "<>", ">", ">=", "<", "<=", "IS NULL", "IS NOT NULL")
PATTERNS = ("LIKE", "~", "~*", "!~", "!~*", "SIMILAR TO", "NOT SIMILAR TO")
AGGREGATES = ("COUNT", "SUM", "AVG", "MIN", "MAX")
EMPTY = sql.SQL("")


def join(items):
    return sql.SQL(", ").join(items)


def require(value, message="Заполните обязательное поле."):
    if not value:
        raise ValueError(message)
    return value


def condition(field, operator, value):
    if operator in ("IS NULL", "IS NOT NULL"):
        return sql.SQL("{} " + operator).format(field), []
    if operator in PATTERNS:
        return sql.SQL("CAST({} AS text) " + operator + " %s").format(field), [value]
    if operator in COMPARISONS:
        return sql.SQL("{} " + operator + " %s").format(field), [value]
    raise ValueError("Выберите оператор сравнения.")


def where(conditions):
    if not conditions:
        return EMPTY
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)


def aggregate(function, field):
    if function not in AGGREGATES:
        raise ValueError("Выберите агрегатную функцию.")
    if not field and function != "COUNT":
        raise ValueError("Выберите столбец для агрегирования.")
    return sql.SQL(function + "({})").format(sql.Identifier(field) if field else sql.SQL("*"))


def select_query(db, table, columns, filters=(), grouping=(), group_mode="GROUP BY",
                 group_sets=(), function="", field="", having=None, order="", direction="ASC"):
    selected = list(map(sql.Identifier, columns))
    conditions, params = [], []
    for column, operator, value in filters:
        predicate, values = condition(sql.Identifier(column), operator, value)
        conditions.append(predicate)
        params.extend(values)
    if grouping or group_mode == "GROUPING SETS":
        if group_mode == "GROUPING SETS":
            require(group_sets, "Добавьте хотя бы один набор группировки.")
            grouping = tuple(dict.fromkeys(column for group in group_sets for column in group))
        selected = list(map(sql.Identifier, grouping))
    aggregate_sql = aggregate(function, field) if function else None
    if aggregate_sql:
        if not grouping and group_mode != "GROUPING SETS":
            selected = []
        selected.append(aggregate_sql + sql.SQL(" AS aggregate_value"))
    if having and not aggregate_sql:
        raise ValueError("Для HAVING выберите агрегатную функцию.")

    query = sql.SQL("SELECT {} FROM {}").format(
        join(selected) if selected else sql.SQL("*"), db.relation(table)
    ) + where(conditions)

    if group_mode == "GROUPING SETS":
        sets = [sql.SQL("({})").format(join(map(sql.Identifier, group))) for group in group_sets]
        query += sql.SQL(" GROUP BY GROUPING SETS ({})").format(join(sets))
    elif grouping:
        fields = join(map(sql.Identifier, grouping))
        if group_mode in ("ROLLUP", "CUBE"):
            fields = sql.SQL(group_mode + "({})").format(fields)
        query += sql.SQL(" GROUP BY ") + fields
    elif group_mode in ("ROLLUP", "CUBE"):
        raise ValueError("Выберите хотя бы один столбец группировки.")

    if having:
        clause, values = condition(aggregate_sql, *having)
        query += sql.SQL(" HAVING ") + clause
        params.extend(values)

    if order:
        if direction not in ("ASC", "DESC"):
            raise ValueError("Выберите порядок сортировки.")
        query += sql.SQL(" ORDER BY {} " + direction).format(sql.Identifier(order))
    return query, params


def join_query(db, left, right, left_key, right_key, kind, filter_side, filter_field,
               operator, value, sort_side, sort_field, direction):
    if kind not in ("INNER", "LEFT", "RIGHT", "FULL") or direction not in ("ASC", "DESC"):
        raise ValueError("Выберите тип соединения и порядок сортировки.")
    left_cols = [(sql.Identifier("l", column), sql.Identifier(f"left_{column}"))
                 for column, in db.columns(left)]
    right_cols = [(sql.Identifier("r", column), sql.Identifier(f"right_{column}"))
                  for column, in db.columns(right)]
    query = sql.SQL("SELECT {} FROM {} AS l " + kind + " JOIN {} AS r ON {} = {}").format(
        join(sql.SQL("{} AS {}").format(*pair) for pair in left_cols + right_cols),
        db.relation(left), db.relation(right), sql.Identifier("l", left_key),
        sql.Identifier("r", right_key),
    )
    params = []
    if filter_field:
        predicate, params = condition(sql.Identifier(filter_side, filter_field), operator, value)
        query += where([predicate])
    if sort_field:
        query += sql.SQL(" ORDER BY {} " + direction).format(sql.Identifier(sort_side, sort_field))
    return query, params


def subquery(db, outer_table, outer_field, operator, quantifier, inner_table, inner_field,
             inner_filter, correlation):
    if quantifier not in ("ANY", "ALL", "EXISTS"):
        raise ValueError("Выберите ANY, ALL или EXISTS.")
    conditions, params = [], []
    if inner_filter:
        field, comparison, value = inner_filter
        predicate, params = condition(sql.Identifier("i", field), comparison, value)
        conditions.append(predicate)
    if correlation:
        outer_key, inner_key = correlation
        conditions.append(sql.SQL("{} = {}").format(
            sql.Identifier("i", inner_key), sql.Identifier("o", outer_key)
        ))
    inner_column = sql.SQL("1") if quantifier == "EXISTS" else sql.Identifier("i", inner_field)
    inner = sql.SQL("SELECT {} FROM {} AS i").format(inner_column, db.relation(inner_table))
    inner += where(conditions)
    if quantifier == "EXISTS":
        predicate = sql.SQL("EXISTS ({})").format(inner)
    else:
        if operator not in COMPARISONS[:6]:
            raise ValueError("Выберите оператор сравнения.")
        predicate = sql.SQL("{} " + operator + " " + quantifier + " ({})").format(
            sql.Identifier("o", outer_field), inner
        )
    query = sql.SQL("SELECT o.* FROM {} AS o").format(db.relation(outer_table)) + where([predicate])
    return query, params


def string_query(db, table, field, operation, argument, count, other_field):
    column = sql.Identifier(field)
    text = sql.SQL("CAST({} AS text)").format(column)
    params = []
    if operation in ("UPPER", "LOWER", "TRIM"):
        expression = sql.SQL(operation + "({})").format(text)
    elif operation == "SUBSTRING":
        start = int(argument)
        if start < 1:
            raise ValueError("Начальная позиция должна быть не меньше 1.")
        expression = sql.SQL("SUBSTRING({} FROM %s FOR %s)").format(text)
        params = [start, count]
    elif operation in ("LPAD", "RPAD"):
        expression = sql.SQL(operation + "({}, %s, %s)").format(text)
        params = [count, argument]
    elif operation == "CONCAT":
        if other_field:
            expression = sql.SQL("CONCAT({}, {})").format(text, sql.Identifier(other_field))
        else:
            expression = sql.SQL("CONCAT({}, %s::text)").format(text)
            params = [argument]
    else:
        raise ValueError("Выберите строковую функцию.")
    query = sql.SQL("SELECT {} AS original_value, {} AS transformed_value FROM {}").format(
        column, expression, db.relation(table)
    )
    return query, params


def case_query(db, table, rules, otherwise):
    require(rules, "Добавьте хотя бы одно условие WHEN.")
    expressions, params = [], []
    for field, operator, value, result in rules:
        predicate, values = condition(sql.Identifier(field), operator, value)
        expressions.append(sql.SQL("WHEN {} THEN %s::text").format(predicate))
        params.extend([*values, result])
    query = sql.SQL("SELECT t.*, CASE {} ELSE %s::text END AS case_result FROM {} AS t").format(
        sql.SQL(" ").join(expressions), db.relation(table)
    )
    return query, [*params, otherwise]


def null_query(db, table, field, operation, value):
    if operation not in ("COALESCE", "NULLIF"):
        raise ValueError("Выберите функцию для NULL.")
    query = sql.SQL("SELECT {} AS original_value, " + operation + "({}, %s) AS null_result FROM {}").format(
        sql.Identifier(field), sql.Identifier(field), db.relation(table)
    )
    return query, [value]


def cte_query(db, blocks, output):
    definitions, params = [], []
    for block_name, source, from_cte, columns, predicate in blocks:
        relation = sql.Identifier(source) if from_cte else db.relation(source)
        selected = join(map(sql.Identifier, columns)) if columns else sql.SQL("*")
        query = sql.SQL("SELECT {} FROM {}").format(selected, relation)
        if predicate:
            field, operator, value = predicate
            clause, values = condition(sql.Identifier(field), operator, value)
            query += where([clause])
            params.extend(values)
        definitions.append(sql.SQL("{} AS ({})").format(sql.Identifier(block_name), query))
    return sql.SQL("WITH {} SELECT * FROM {}").format(join(definitions), sql.Identifier(output)), params


def window_query(db, table, function, column, partitions, order, offset):
    if function not in ("RANK", "LAG", "LEAD"):
        raise ValueError("Выберите оконную функцию.")
    require(order, "Добавьте хотя бы один столбец сортировки.")
    ordering = []
    for field, direction in order:
        if direction not in ("ASC", "DESC"):
            raise ValueError("Выберите порядок сортировки.")
        ordering.append(sql.SQL("{} " + direction).format(sql.Identifier(field)))
    over = EMPTY
    if partitions:
        over = sql.SQL("PARTITION BY {} ").format(join(map(sql.Identifier, partitions)))
    over += sql.SQL("ORDER BY {}").format(join(ordering))
    if function == "RANK":
        expression, params = sql.SQL("RANK()"), []
    else:
        expression = sql.SQL(function + "({}, %s)").format(sql.Identifier(column))
        params = [offset]
    query = sql.SQL("SELECT t.*, {} OVER ({}) AS window_value FROM {} AS t").format(
        expression, over, db.relation(table)
    )
    return query, params
