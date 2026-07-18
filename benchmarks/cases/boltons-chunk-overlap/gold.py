def chunk_ranges(input_size, chunk_size, input_offset=0, overlap_size=0, align=False):
    input_size = _validate_positive_int(
        input_size, 'input_size', strictly_positive=False)
    chunk_size = _validate_positive_int(chunk_size, 'chunk_size')
    input_offset = _validate_positive_int(
        input_offset, 'input_offset', strictly_positive=False)
    overlap_size = _validate_positive_int(
        overlap_size, 'overlap_size', strictly_positive=False)

    input_stop = input_offset + input_size

    if align:
        initial_chunk_len = chunk_size - \
            input_offset % (chunk_size - overlap_size)
        if initial_chunk_len != overlap_size:
            yield (input_offset, min(input_offset + initial_chunk_len, input_stop))
            if input_offset + initial_chunk_len >= input_stop:
                return
            input_offset = input_offset + initial_chunk_len - overlap_size

    for i in range(input_offset, input_stop, chunk_size - overlap_size):
        yield (i, min(i + chunk_size, input_stop))

        if i + chunk_size >= input_stop:
            return
