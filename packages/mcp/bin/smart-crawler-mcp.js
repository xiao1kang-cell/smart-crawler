#!/usr/bin/env node
"use strict";

const args = process.argv.slice(2);

function readFlag(name, fallback = null) {
  const i = args.indexOf(`--${name}`);
  if (i === -1 || i + 1 >= args.length) return fallback;
  return args[i + 1];
}

function hasFlag(name) {
  return args.includes(`--${name}`);
}

function usage() {
  console.log(`smart-crawler MCP install helper

Usage:
  npx -y @smart-crawler/mcp install --client codex --env-var SMARTCRAWLER_API_KEY
  npx -y @smart-crawler/mcp install --client claude --url https://smartcrawler.io/mcp
  npx -y @smart-crawler/mcp install --client cursor --url http://127.0.0.1:8077/mcp

Options:
  --client codex|claude|cursor   Client config to print. Default: codex
  --url URL                      MCP endpoint. Default: https://smartcrawler.io/mcp
  --env-var NAME                 Environment variable holding sck_ key. Default: SMARTCRAWLER_API_KEY
  --local                        Shortcut for http://127.0.0.1:8077/mcp

This helper never prints or stores your API key. It prints copy/paste config only.`);
}

function main() {
  const command = args[0] || "help";
  if (command === "help" || hasFlag("help")) {
    usage();
    return;
  }
  if (command !== "install") {
    console.error(`Unknown command: ${command}`);
    usage();
    process.exit(2);
  }

  const client = readFlag("client", "codex");
  const url = hasFlag("local") ? "http://127.0.0.1:8077/mcp" :
    readFlag("url", "https://smartcrawler.io/mcp");
  const envVar = readFlag("env-var", "SMARTCRAWLER_API_KEY");

  if (!["codex", "claude", "cursor"].includes(client)) {
    console.error("--client must be codex, claude, or cursor");
    process.exit(2);
  }

  console.log(`# smart-crawler MCP (${client})`);
  console.log(`# Endpoint: ${url}`);
  console.log(`# Set your key first: export ${envVar}=sck_xxx`);
  console.log("");

  if (client === "codex") {
    console.log(`codex mcp add smart-crawler \\
  --url ${url} \\
  --bearer-token-env-var ${envVar}`);
    return;
  }

  console.log(`# ${client} JSON headers may not expand environment variables.`);
  console.log("# Replace sck_xxx with your real key in the app config, or use the Codex command above for env-var auth.");
  console.log("");
  const headers = { Authorization: "Bearer sck_xxx" };
  const block = {
    mcpServers: {
      "smart-crawler": client === "claude"
        ? { type: "http", url, headers }
        : { url, headers },
    },
  };
  console.log(JSON.stringify(block, null, 2));
}

main();
