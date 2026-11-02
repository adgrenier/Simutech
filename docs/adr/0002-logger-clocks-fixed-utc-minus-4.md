# Treat logger timestamps as fixed UTC-4 and convert to UTC

The Logger programs sync their clocks over NTP with a fixed -4 hour offset, so Raw file timestamps are UTC-4 all year with no daylight-saving change, not UTC. All Summaries are stamped in UTC by adding four hours, using a per-Logger offset setting. Assuming UTC would shift every value in PI by four hours; following the local zone would break twice a year. The Nacelle and Tower_Top programs still need to be checked for the same offset.
