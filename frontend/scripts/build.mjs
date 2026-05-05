import { copyFileSync, existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");
const assets = join(dist, "assets");

rmSync(dist, { recursive: true, force: true });
mkdirSync(assets, { recursive: true });

await build({
  entryPoints: ["src/main.tsx"],
  bundle: true,
  format: "esm",
  target: "es2020",
  jsx: "automatic",
  loader: {
    ".ts": "ts",
    ".tsx": "tsx",
    ".css": "css",
  },
  define: {
    "process.env.NODE_ENV": '"production"',
  },
  outfile: "dist/assets/main.js",
  minify: true,
  logLevel: "info",
  absWorkingDir: root,
});

writeFileSync(
  join(dist, "index.html"),
  `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <meta name="referrer" content="no-referrer" />
    <meta name="theme-color" content="#eef3f8" />
    <title>order-song</title>
    <script type="module" crossorigin src="./assets/main.js"></script>
    <link rel="stylesheet" crossorigin href="./assets/main.css">
    <link rel="icon" href="./favicon.ico" />
  </head>
  <body>
    <div id="root"></div>
  </body>
</html>
`,
);

if (existsSync(join(root, "favicon.ico"))) {
  copyFileSync(join(root, "favicon.ico"), join(dist, "favicon.ico"));
}
