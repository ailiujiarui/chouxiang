def chunked(iterable, n, strict=False):
    iterator = iter(partial(take, n, iter(iterable)), [])
    if strict:
        if n is None:
            raise ValueError('n must not be None when using strict mode.')

        def checked():
            for chunk in iterator:
                if len(chunk) != n:
                    raise ValueError('iterable is not divisible by n.')
                yield chunk

        return checked()
    return iterator
