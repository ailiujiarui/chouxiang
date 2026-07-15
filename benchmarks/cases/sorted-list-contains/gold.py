class SortedList:
    def __contains__(self, value):
        _maxes = self._maxes
        if not _maxes:
            return False
        pos = bisect_left(_maxes, value)
        if pos == len(_maxes):
            return False
        _lists = self._lists
        idx = bisect_left(_lists[pos], value)
        return _lists[pos][idx] == value
