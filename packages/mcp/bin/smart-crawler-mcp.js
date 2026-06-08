#!/usr/bin/env node
"use strict";

const DEFAULT_REMOTE_URL = "https://smartcrawler.io/mcp";
const DEFAULT_LOCAL_URL = "http://127.0.0.1:8077/mcp";
const CLIENTS = new Set(["codex", "claude", "cursor"]);

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) {
      out._.push(arg);
      continue;
    }
    const name = arg.slice(2);
    if (["help", "local", "json", "show-key-placeholder"].includes(name)) {
      out[name] = true;
      continue;
    }
    if (i + 1 >= argv.length || argv[i + 1].startsWith("--")) {
      throw new Error(`--${name} requires a value`);
    }
    out[name] = argv[i + 1];
    i += 1;
  }
  return out;
}

function usage() {
  console.log(`smart-crawler MCP install helper

Usage:
  npx -y smart-crawler-mcp install --client codex --env-var SMARTCRAWLER_API_KEY
  npx -y smart-crawler-mcp install --client claude --url https://smartcrawler.io/mcp
  npx -y smart-crawler-mcp install --client cursor --local
  npx -y smart-crawler-mcp dxt --env-var SMARTCRAWLER_API_KEY

Commands:
  install   Print Codex / Claude / Cursor MCP config.
  dxt       Print a Claude Desktop .dxt manifest draft.
  doctor    Check inputs and print the resolved endpoint/name/env var.
  help      Show this help.

Options:
  --client codex|claude|cursor   Client config to print. Default: codex
  --url URL                      MCP endpoint. Default: ${DEFAULT_REMOTE_URL}
  --local                        Shortcut for ${DEFAULT_LOCAL_URL}
  --name NAME                    MCP server name. Default: smart-crawler
  --env-var NAME                 Environment variable holding sck_ key.
                                Default: SMARTCRAWLER_API_KEY
                                With --local: SMARTCRAWLER_LOCAL_API_KEY
  --json                         Print only machine-readable JSON where possible.

This helper never prints or stores your real API key.`);
}

function resolveConfig(options) {
  const client = options.client || "codex";
  if (!CLIENTS.has(client)) {
    throw new Error("--client must be codex, claude, or cursor");
  }
  const local = Boolean(options.local);
  const url = local ? DEFAULT_LOCAL_URL : (options.url || DEFAULT_REMOTE_URL);
  validateUrl(url);
  const name = options.name || (local ? "smart-crawler-local" : "smart-crawler");
  validateServerName(name);
  const envVar = options["env-var"] || (
    local ? "SMARTCRAWLER_LOCAL_API_KEY" : "SMARTCRAWLER_API_KEY"
  );
  validateEnvVar(envVar);
  return { client, envVar, local, name, url };
}

function validateUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(`Invalid --url: ${value}`);
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("--url must start with http:// or https://");
  }
}

function validateEnvVar(value) {
  if (!/^[A-Z_][A-Z0-9_]*$/.test(value)) {
    throw new Error("--env-var must be an uppercase environment variable name");
  }
}

function validateServerName(value) {
  if (!/^[a-zA-Z0-9_.-]+$/.test(value)) {
    throw new Error("--name may only contain letters, numbers, dot, dash, and underscore");
  }
}

function bearerPlaceholder(envVar) {
  return `Bearer \${${envVar}}`;
}

function clientJson(config) {
  const headers = { Authorization: bearerPlaceholder(config.envVar) };
  return {
    mcpServers: {
      [config.name]: config.client === "claude"
        ? { type: "http", url: config.url, headers }
        : { url: config.url, headers },
    },
  };
}

function codexCommand(config) {
  return `codex mcp add ${config.name} \\
  --url ${config.url} \\
  --bearer-token-env-var ${config.envVar}`;
}

function claudeCommand(config) {
  return `claude mcp add --scope user --transport http ${config.name} \\
  ${config.url} \\
  --header "Authorization: Bearer \${${config.envVar}}"`;
}

function printInstall(config, jsonOnly) {
  if (jsonOnly) {
    let payload;
    if (config.client === "codex") {
      payload = { command: codexCommand(config), ...config };
    } else if (config.client === "claude") {
      payload = { command: claudeCommand(config), config: clientJson(config), ...config };
    } else {
      payload = { config: clientJson(config), ...config };
    }
    console.log(JSON.stringify(payload, null, 2));
    return;
  }

  console.log(`# smart-crawler MCP (${config.client})`);
  console.log(`# Name: ${config.name}`);
  console.log(`# Endpoint: ${config.url}`);
  console.log(`# Set your key first: export ${config.envVar}=sck_xxx`);
  console.log("");

  if (config.client === "codex") {
    console.log(codexCommand(config));
    return;
  }

  if (config.client === "claude") {
    console.log("# Run this command (--scope user makes it visible in every project):");
    console.log(claudeCommand(config));
    console.log("");
    console.log("# Or paste this JSON into your Claude MCP config manually.");
    console.log("# Replace ${...} with the real key if your client does not expand environment variables.");
    console.log("");
    console.log(JSON.stringify(clientJson(config), null, 2));
    return;
  }

  console.log(`# Paste this JSON into your ${config.client} MCP config.`);
  console.log("# Replace ${...} with the real key if your client does not expand environment variables.");
  console.log("");
  console.log(JSON.stringify(clientJson(config), null, 2));
}

function printDoctor(config, jsonOnly) {
  const payload = {
    ok: true,
    client: config.client,
    name: config.name,
    url: config.url,
    env_var: config.envVar,
    local: config.local,
    primary_tools: ["query_warehouse", "scrape_url", "crawl_site"],
  };
  if (jsonOnly) {
    console.log(JSON.stringify(payload, null, 2));
    return;
  }
  console.log("smart-crawler MCP config looks valid");
  console.log(`  client: ${payload.client}`);
  console.log(`  name:   ${payload.name}`);
  console.log(`  url:    ${payload.url}`);
  console.log(`  env:    ${payload.env_var}`);
  console.log(`  tools:  ${payload.primary_tools.join(", ")}`);
}

function dxtManifest(config) {
  return {
    name: config.name,
    display_name: "smart-crawler",
    version: "0.1.0",
    description: "Agent-first ecommerce crawler with warehouse-first search, memory, and cost-aware MCP tools.",
    server: {
      type: "http",
      url: config.url,
      headers: {
        Authorization: bearerPlaceholder(config.envVar),
      },
    },
    tools: {
      primary: ["query_warehouse", "scrape_url", "crawl_site"],
    },
  };
}

function printDxt(config, jsonOnly) {
  const manifest = dxtManifest(config);
  if (jsonOnly) {
    console.log(JSON.stringify(manifest, null, 2));
    return;
  }
  console.log("# Claude Desktop .dxt manifest draft");
  console.log("# Save as manifest.json inside the .dxt package source directory.");
  console.log("");
  console.log(JSON.stringify(manifest, null, 2));
}

function run(argv = process.argv.slice(2)) {
  let options;
  try {
    options = parseArgs(argv);
    const command = options._[0] || "help";
    if (command === "help" || options.help) {
      usage();
      return 0;
    }
    if (!["install", "doctor", "dxt"].includes(command)) {
      throw new Error(`Unknown command: ${command}`);
    }
    const config = resolveConfig(options);
    if (command === "install") printInstall(config, Boolean(options.json));
    if (command === "doctor") printDoctor(config, Boolean(options.json));
    if (command === "dxt") printDxt(config, Boolean(options.json));
    return 0;
  } catch (err) {
    console.error(err.message);
    console.error("");
    usage();
    return 2;
  }
}

if (require.main === module) {
  process.exitCode = run();
}

module.exports = {
  CLIENTS,
  DEFAULT_LOCAL_URL,
  DEFAULT_REMOTE_URL,
  bearerPlaceholder,
  clientJson,
  claudeCommand,
  codexCommand,
  dxtManifest,
  parseArgs,
  resolveConfig,
  run,
};
