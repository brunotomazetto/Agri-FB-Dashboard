#!/bin/sh
set -e

if [ -z "$SUPABASE_URL" ]; then
  echo "Error: SUPABASE_URL environment variable is not set." >&2
  exit 1
fi

if [ -z "$SUPABASE_ANON_KEY" ]; then
  echo "Error: SUPABASE_ANON_KEY environment variable is not set." >&2
  exit 1
fi

if [ -z "$NEWS_API_URL" ]; then
  echo "Error: NEWS_API_URL environment variable is not set." >&2
  exit 1
fi

sed -i "s|__SUPABASE_URL__|${SUPABASE_URL}|g" index.html
sed -i "s|__SUPABASE_ANON_KEY__|${SUPABASE_ANON_KEY}|g" index.html
sed -i "s|__NEWS_API_URL__|${NEWS_API_URL}|g" "Market Watch/Sector News/sector_news.html"

echo "Build complete."
