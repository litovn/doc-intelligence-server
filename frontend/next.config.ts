import type { NextConfig } from "next";

// `next build` produces the static export the Python backend serves;
// `next dev` runs a real server and proxies /api/* to it instead. Next.js
// treats "output: export" and "rewrites" as mutually exclusive (it warns,
// and the rewrite stops applying, whenever both are present at once — this
// isn't just a build-time-only check) so each lives in its own branch
// instead of both being set together.
const isDev = process.env.NODE_ENV === "development";

const nextConfig: NextConfig = isDev
  ? {
      async rewrites() {
        return [
          {
            source: "/api/:path*",
            destination: "http://localhost:8000/api/:path*",
          },
        ];
      },
    }
  : {
      output: "export",
      // Trailing slashes so the static export writes `tags/index.html`
      // etc., which a plain static file server (FastAPI's StaticFiles)
      // resolves directly without needing per-route rewrite rules.
      trailingSlash: true,
    };

export default nextConfig;
