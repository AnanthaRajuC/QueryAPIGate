// The "experimental" tag (BACKLOG #64): outside the compatibility promise, may change in any minor release.
export function ExperimentalTag() {
  return (
    <span
      className="tag experimental"
      title="Experimental: may change in any minor release, always noted in the changelog"
    >
      experimental
    </span>
  );
}
