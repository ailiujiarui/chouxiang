def first(iterable, default=_marker):
    for item in iterable:
        return item
    if default is _marker:
        raise ValueError(
            'first() was called on an empty iterable, '
            'and no default value was provided.'
        )
    return default
