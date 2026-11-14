/** Human-readable file size, e.g. ``"1.5 MB"``. */
export const formatBytes = (bytes: number) => {
  if (!bytes) return "0 Bytes";
  const units = ["Bytes", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1
  );
  const value = bytes / Math.pow(1024, exponent);
  const rounded =
    value >= 10 || exponent === 0 ? value.toFixed(0) : value.toFixed(1);
  return `${rounded} ${units[exponent]}`;
};
