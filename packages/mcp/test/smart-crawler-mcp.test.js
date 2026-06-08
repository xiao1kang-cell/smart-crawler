"use strict";

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");

const helper = require("../bin/smart-crawler-mcp.js");

describe("smart-crawler MCP helper", () => {
  it("resolves remote Codex config by default", () => {
    const config = helper.resolveConfig(helper.parseArgs(["install"]));

    assert.equal(config.client, "codex");
    assert.equal(config.name, "smart-crawler");
    assert.equal(config.url, helper.DEFAULT_REMOTE_URL);
    assert.equal(config.envVar, "SMARTCRAWLER_API_KEY");
  });

  it("uses local name and env var with --local", () => {
    const config = helper.resolveConfig(helper.parseArgs([
      "install",
      "--client",
      "cursor",
      "--local",
    ]));

    assert.equal(config.client, "cursor");
    assert.equal(config.name, "smart-crawler-local");
    assert.equal(config.url, helper.DEFAULT_LOCAL_URL);
    assert.equal(config.envVar, "SMARTCRAWLER_LOCAL_API_KEY");
  });

  it("prints Codex command using bearer-token env var", () => {
    const config = helper.resolveConfig(helper.parseArgs([
      "install",
      "--client",
      "codex",
      "--name",
      "smart-crawler-staging",
      "--env-var",
      "SMARTCRAWLER_STAGING_API_KEY",
    ]));

    assert.equal(
      helper.codexCommand(config),
      "codex mcp add smart-crawler-staging \\\n" +
        "  --url https://smartcrawler.io/mcp \\\n" +
        "  --bearer-token-env-var SMARTCRAWLER_STAGING_API_KEY",
    );
  });

  it("generates Claude HTTP MCP JSON", () => {
    const config = helper.resolveConfig(helper.parseArgs([
      "install",
      "--client",
      "claude",
    ]));
    const json = helper.clientJson(config);

    assert.deepEqual(json.mcpServers["smart-crawler"], {
      type: "http",
      url: "https://smartcrawler.io/mcp",
      headers: {
        Authorization: "Bearer ${SMARTCRAWLER_API_KEY}",
      },
    });
  });

  it("prints Claude command with --scope user so it is visible in every project", () => {
    const config = helper.resolveConfig(helper.parseArgs([
      "install",
      "--client",
      "claude",
    ]));

    assert.equal(
      helper.claudeCommand(config),
      "claude mcp add --scope user --transport http smart-crawler \\\n" +
        "  https://smartcrawler.io/mcp \\\n" +
        '  --header "Authorization: Bearer ${SMARTCRAWLER_API_KEY}"',
    );
  });

  it("generates Cursor MCP JSON", () => {
    const config = helper.resolveConfig(helper.parseArgs([
      "install",
      "--client",
      "cursor",
      "--local",
    ]));
    const json = helper.clientJson(config);

    assert.deepEqual(json.mcpServers["smart-crawler-local"], {
      url: "http://127.0.0.1:8077/mcp",
      headers: {
        Authorization: "Bearer ${SMARTCRAWLER_LOCAL_API_KEY}",
      },
    });
  });

  it("generates dxt manifest draft", () => {
    const config = helper.resolveConfig(helper.parseArgs([
      "dxt",
      "--env-var",
      "SMARTCRAWLER_API_KEY",
    ]));
    const manifest = helper.dxtManifest(config);

    assert.equal(manifest.server.type, "http");
    assert.equal(manifest.server.url, "https://smartcrawler.io/mcp");
    assert.equal(
      manifest.server.headers.Authorization,
      "Bearer ${SMARTCRAWLER_API_KEY}",
    );
    assert.deepEqual(manifest.tools.primary, [
      "query_warehouse",
      "scrape_url",
      "crawl_site",
    ]);
  });

  it("rejects unsupported clients and env names", () => {
    assert.throws(
      () => helper.resolveConfig(helper.parseArgs(["install", "--client", "vim"])),
      /--client must be/,
    );
    assert.throws(
      () => helper.resolveConfig(helper.parseArgs(["install", "--env-var", "bad-key"])),
      /--env-var/,
    );
  });
});
