df['price'] = df['price'].str.replace(r'£', '', regex=True)  #make function later on??
df['unit'] = df['weight'].str.extract(r'([a-zA-Z]+\b)')
df['product_name'] = df['product_name'].str.extract('^(\D+)')
df['weight'] = df['weight'].str.extract(r'(\d+)')
df = df.astype({'price':'float', 'unit':'string','product_name':'string','weight':'float'})
df['price_per_unit'] = df['price']/df['weight']
#df['Weight'] = df['Weight'].str.split('(')[0]
#get list of aldi brands to extract from prodcut name string 
def remove_brands_regex(df, brands_list, column_name='product_name'):
    # Escape special regex characters and join with '|'
    pattern = '|'.join(map(re.escape, sorted(brands_list, key=len, reverse=True)))
    # Compile pattern once for better performance
    regex = re.compile(f'({pattern})')
    # Use vectorized string operations
    df[column_name] = (df[column_name]
                            .str.replace(regex, '', regex=True)
                            .str.strip()
                            .str.replace(r'\s+', ' ', regex=True))
    return df
df_cleaned = remove_brands_regex(df, aldi_brands)