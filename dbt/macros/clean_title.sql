{#
    Strips pack-size / weight / volume / duration quantifiers from an already-normalized
    title (see normalize_text), so they don't dominate downstream embedding similarity.
    Ported from remove_quantities_batch in notebooks/embeddings.ipynb. Applied in order:
    multiplier phrases ("2 x 400g") -> duration ranges ("1 2 years") -> single quantities
    ("500g", "8 pack", "12 months") -> whitespace collapse.

    normalize_text() strips punctuation (incl. the decimal point) before this macro runs,
    so "1.5l" becomes "15l" first; the \d+ quantifier below still matches and removes it.
#}
{% macro clean_title(col) %}
trim(
    regexp_replace(
        regexp_replace(
            regexp_replace(
                {{ normalize_text(col) }},
            '\b\d+\s?x\s?\d+\s?(?:g|kg|mls?|cl|l|litres?|oz|mm|m|family pack|twin pack|megapack|pack|pk|pieces?|portions|servings|days?|hours?|months?|years?|weeks?|piece pack)\b', ' ', 'g'),
        '\b\d+\s+\d+\s?(?:days?|hours?|months?|years?|weeks?)\b', ' ', 'g'),
    '\b\d+\s?(?:g|kg|mls?|cl|l|litres?|oz|mm|m|family pack|twin pack|megapack|pack|pk|pieces?|portions|servings|days?|hours?|months?|years?|weeks?)\b', ' ', 'g')
)
{% endmacro %}
