import { Link } from 'react-router';

export function NotFound() {
  return (
    <div className="flex flex-col gap-2">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <Link to="/" className="text-sm text-accent underline">
        Back to the overview
      </Link>
    </div>
  );
}
