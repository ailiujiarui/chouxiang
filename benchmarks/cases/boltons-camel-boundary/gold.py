def camel2under(camel_string):
    return _camel2under_re.sub(r'_\1', camel_string).lower()
