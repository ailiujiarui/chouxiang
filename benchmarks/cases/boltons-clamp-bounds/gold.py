def clamp(x, lower=float('-inf'), upper=float('inf')):
    if upper < lower:
        raise ValueError('expected upper bound (%r) >= lower bound (%r)' % (upper, lower))
    return min(max(x, lower), upper)
