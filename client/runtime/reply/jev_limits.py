"""Size bound shared by every JEV request."""

# The JEV upstream accepts about 32k tokens; Chinese JSON averages ~2.7 bytes per
# token, so 80 KiB stays just inside it. Every JEV module uses this one bound.
JEV_MAX_INPUT_BYTES = 80 * 1024
