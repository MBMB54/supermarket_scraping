{# Column descriptions shared by several models. Reference with {{ doc('col_<name>') }}. #}

{% docs col_id %}Retailer's own product identifier (Tesco tpnc, Aldi sku, SuperValu/Dunnes product id). Unique within a retailer, not across retailers.{% enddocs %}

{% docs col_supermarket %}Retailer the row came from: aldi, tesco, dunnes or supervalu.{% enddocs %}

{% docs col_title %}Product title, lower-cased with accents, punctuation and percent signs normalised (see macro normalize_text). NULL when the scrape returned no payload.{% enddocs %}

{% docs col_title_cleaned %}Normalised title with pack-size, weight and duration quantifiers removed (see macro clean_title); used for title embeddings.{% enddocs %}

{% docs col_brand %}Normalised brand name.{% enddocs %}

{% docs col_is_own_brand %}TRUE for the retailer's own-label products. NULL where the retailer does not expose it (Tesco, Aldi).{% enddocs %}

{% docs col_item_description %}Product description as scraped. Dunnes descriptions contain HTML.{% enddocs %}

{% docs col_category_1 %}Top level of the retailer's category tree (whitespace-normalised). SuperValu/Dunnes skip the root 'Grocery' level.{% enddocs %}

{% docs col_category_2 %}Second level of the retailer's category tree.{% enddocs %}

{% docs col_category_3 %}Third level of the retailer's category tree. NULL for Aldi.{% enddocs %}

{% docs col_category_4 %}Fourth level of the retailer's category tree. NULL for Aldi and Dunnes.{% enddocs %}

{% docs col_is_discount %}TRUE when a per-unit price cut applies (was_price present for SuperValu/Dunnes, price_cut promotion for Tesco, was-price shown for Aldi).{% enddocs %}

{% docs col_is_promotion %}TRUE when a multibuy-style promotion applies (3-for-2, bundle deals). Can be TRUE together with is_discount. NULL for Aldi.{% enddocs %}

{% docs col_price %}Current effective shelf price in EUR for one unit; already reduced when a price cut applies.{% enddocs %}

{% docs col_was_price %}Original price in EUR before a price cut; NULL when no price cut applies.{% enddocs %}

{% docs col_selling_size %}Pack quantity in the retailer's own unit (the number in 500 g, 1.5 l, ...).{% enddocs %}

{% docs col_unit %}Lower-cased retailer unit for selling_size (g, kg, ml, l, cl, m, ...).{% enddocs %}

{% docs col_unit_qty_normalised %}selling_size converted to kg, l or m. For counted items (blank unit, Tesco sheets, Aldi each/pack) it is the item count, so price_per_unit_normalised is per item; 1.0 (price per pack) when there is no count, the unit is not a count (e.g. m2 area), or the title states a pack count (N pack, Npk, pack of N, N x) that differs from the count.{% enddocs %}

{% docs col_unit_normalised %}Normalised unit: kg, l, m or each.{% enddocs %}

{% docs col_unit_price %}Retailer-quoted price per unit of measure in EUR. NULL for Aldi, which has no usable value.{% enddocs %}

{% docs col_price_per_unit_normalised %}price divided by unit_qty_normalised: EUR per kg / l / m rounded to 2 dp, or EUR per item (unit_normalised = each) rounded to 4 dp so small per-item prices such as sheets are not rounded to zero.{% enddocs %}

{% docs col_promotion_description %}Text of the first promotion on the product.{% enddocs %}

{% docs col_promotion_type %}Retailer promotion type of the first promotion (e.g. BulkPromotion, BundlePromotion).{% enddocs %}

{% docs col_promotion_qualities %}Tesco promotion quality tags (price_cut, multibuy, ...). NULL for other retailers.{% enddocs %}

{% docs col_promotion_start_date %}First promotion start date, converted from UTC to Europe/Dublin.{% enddocs %}

{% docs col_promotion_end_date %}First promotion end date, converted from UTC to Europe/Dublin.{% enddocs %}

{% docs col_discount_start_date %}Start of a temporary price reduction (TPR). SuperValu/Dunnes only.{% enddocs %}

{% docs col_discount_end_date %}End of a temporary price reduction (TPR). SuperValu/Dunnes only.{% enddocs %}

{% docs col_ingredients %}Ingredients as a single cleaned string; NULL when absent or a placeholder such as 'N/A'.{% enddocs %}

{% docs col_contains_allergens %}Allergens the product contains.{% enddocs %}

{% docs col_may_contain_allergens %}Allergens the product may contain through cross-contamination.{% enddocs %}

{% docs col_dietary_flags %}Raw dietary tags from the retailer (lifestyle tags, food icons). NULL for Aldi.{% enddocs %}

{% docs col_is_vegan %}Suitable for vegans. Derived from retailer attribute and tags; NULL for Aldi.{% enddocs %}

{% docs col_is_vegetarian %}Suitable for vegetarians. NULL for Aldi.{% enddocs %}

{% docs col_is_gluten_free %}Gluten free by any of the retailer's gluten-free signals. NULL for Aldi.{% enddocs %}

{% docs col_is_organic %}Organic by retailer tag, certification badge or organic category path. NULL for Aldi.{% enddocs %}

{% docs col_is_low_fat %}Low fat by retailer tag, title wording or nutrition panel (signals differ per retailer, see model SQL). NULL for Aldi.{% enddocs %}

{% docs col_is_kosher %}Kosher-certified per retailer tags. NULL for Aldi.{% enddocs %}

{% docs col_is_halal %}Halal per retailer tags. NULL for Aldi.{% enddocs %}

{% docs col_is_dairy_free %}Dairy or lactose free. SuperValu only; NULL elsewhere.{% enddocs %}

{% docs col_is_sugar_free %}Sugar free. SuperValu only; NULL elsewhere.{% enddocs %}

{% docs col_has_no_added_sugar %}Carries a no-added-sugar claim. SuperValu only; NULL elsewhere.{% enddocs %}

{% docs col_is_diabetic_friendly %}Tagged suitable for diabetics. SuperValu only; NULL elsewhere.{% enddocs %}

{% docs col_image_url %}Product image URL.{% enddocs %}

{% docs col_is_product_available %}TRUE when the product is currently purchasable.{% enddocs %}

{% docs col_scraped_date %}Scrape date: the run_date of the raw S3 partition the row came from.{% enddocs %}
