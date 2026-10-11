"""Minimal Prometheus remote-write encoder (protobuf + snappy), no deps
beyond `requests` and `snappy`.

Only implements the small subset of the write request we need:
  WriteRequest { repeated TimeSeries timeseries = 1; }
  TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2; }
  Label        { string name = 1; string value = 2; }
  Sample       { double value = 1; int64 timestamp = 2; }
"""
import struct

import snappy


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _len_delim(field, payload):
    return _tag(field, 2) + _varint(len(payload)) + payload


def _label(name, value):
    return _len_delim(1, name.encode()) + _len_delim(2, value.encode())


def _sample(value, ts_ms):
    # field 1 double (wire type 1), field 2 int64 varint (wire type 0)
    return _tag(1, 1) + struct.pack("<d", value) + _tag(2, 0) + _varint(ts_ms)


class TimeSeries:
    def __init__(self, name, samples, labels=None):
        """samples: list of (timestamp_seconds, value)"""
        self.name = name
        self.labels = {"__name__": name, **(labels or {})}
        # sort by timestamp as Prometheus requires
        self.samples = sorted(samples, key=lambda x: x[0])

    def encode(self):
        body = b""
        for k, v in self.labels.items():
            body += _len_delim(1, _label(k, v))
        for ts, val in self.samples:
            body += _len_delim(2, _sample(float(val), int(ts) * 1000))
        return _len_delim(1, body)


def write_request(series_list):
    body = b""
    for ts in series_list:
        body += ts.encode()
    return snappy.compress(body)


def remote_write(url, compressed_body, timeout=60):
    import requests

    return requests.post(
        url,
        data=compressed_body,
        headers={
            "Content-Encoding": "snappy",
            "Content-Type": "application/x-protobuf",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
        },
        timeout=timeout,
    )
