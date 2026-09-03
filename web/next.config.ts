import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  turbopack: {
    // Pin the root to web/. Next infers it from the nearest lockfile, which on
    // a dev machine can land outside the repository entirely - it picked up a
    // stray package-lock.json in the home directory here. Vercel builds from
    // web/ as the Root Directory, so this makes local match CI.
    root: path.join(__dirname),
  },
};

export default nextConfig;
