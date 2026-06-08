#!/usr/bin/env node

/**
 * discover_apis.cjs
 * GCP API Discovery Tool for Antigravity Skill
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

// Helper to make HTTPS requests
function fetchUrl(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { 'User-Agent': 'Antigravity-Skill-Agent' } }, (res) => {
      if (res.statusCode !== 200) {
        reject(new Error(`Failed to fetch ${url}: Status ${res.statusCode}`));
        return;
      }
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => { resolve(data); });
    }).on('error', reject);
  });
}

function parseArgs() {
  const args = process.argv.slice(2);
  const options = {
    list: false,
    details: null,
    search: '',
    format: 'markdown'
  };

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--list') {
      options.list = true;
    } else if (args[i] === '--details') {
      options.details = args[++i];
    } else if (args[i] === '--search') {
      options.search = args[++i];
    } else if (args[i] === '--format') {
      options.format = args[++i];
    } else if (args[i] === '--help' || args[i] === '-h') {
      printHelp();
      process.exit(0);
    }
  }
  return options;
}

function printHelp() {
  console.log(`GCP API Discovery Script

Usage:
  node discover_apis.cjs --list [--search <query>] [--format <markdown|json>]
  node discover_apis.cjs --details <api_id> [--format <markdown|json>]

Options:
  --list               List all APIs from Google API Discovery Service
  --search <query>     Filter APIs by name, title, or description
  --details <api_id>   Fetch details for a specific API (e.g. compute:v1)
  --format <format>    Output format: 'markdown' (default) or 'json'
`);
}

async function handleList(options) {
  try {
    const dataRaw = await fetchUrl('https://www.googleapis.com/discovery/v1/apis');
    const directory = JSON.parse(dataRaw);
    
    let items = directory.items || [];
    
    // Filter to GCP/Google Cloud APIs by checking common names or description clues
    // or just let the search query handle it.
    if (options.search) {
      const q = options.search.toLowerCase();
      items = items.filter(item => 
        (item.name && item.name.toLowerCase().includes(q)) ||
        (item.title && item.title.toLowerCase().includes(q)) ||
        (item.description && item.description.toLowerCase().includes(q))
      );
    }

    if (options.format === 'json') {
      console.log(JSON.stringify(items, null, 2));
      return;
    }

    // Markdown Output
    console.log(`# GCP API Discovery List\n`);
    console.log(`Found ${items.length} matching APIs:\n`);
    console.log(`| API ID | Title | Version | Description | Preferred? |`);
    console.log(`|---|---|---|---|---|`);
    for (const item of items) {
      const preferredMark = item.preferred ? '✅' : '❌';
      console.log(`| \`${item.id}\` | ${item.title} | \`${item.version}\` | ${item.description || 'N/A'} | ${preferredMark} |`);
    }
  } catch (error) {
    console.error(`Error listing APIs: ${error.message}`);
    process.exit(1);
  }
}

// Recursively traverse resources to extract endpoint paths
function extractResources(resources, prefix = '') {
  let list = [];
  for (const [name, def] of Object.entries(resources)) {
    const resourcePath = prefix ? `${prefix}.${name}` : name;
    
    const methods = Object.keys(def.methods || {});
    if (methods.length > 0) {
      list.push({
        resource: resourcePath,
        methods: methods
      });
    }
    
    if (def.resources) {
      list = list.concat(extractResources(def.resources, resourcePath));
    }
  }
  return list;
}

async function handleDetails(options) {
  try {
    const apiId = options.details;
    const dataRaw = await fetchUrl('https://www.googleapis.com/discovery/v1/apis');
    const directory = JSON.parse(dataRaw);
    
    const item = (directory.items || []).find(x => x.id === apiId);
    if (!item) {
      console.error(`API ID '${apiId}' not found in Discovery directory.`);
      process.exit(1);
    }

    const docRaw = await fetchUrl(item.discoveryRestUrl);
    const doc = JSON.parse(docRaw);

    const resources = doc.resources ? extractResources(doc.resources) : [];

    if (options.format === 'json') {
      console.log(JSON.stringify({
        id: doc.id,
        name: doc.name,
        version: doc.version,
        title: doc.title,
        baseUrl: doc.baseUrl,
        resources: resources
      }, null, 2));
      return;
    }

    // Markdown output
    console.log(`# API Details: ${doc.title} (${doc.id})`);
    console.log(`\n**Description**: ${doc.description || 'No description provided.'}`);
    console.log(`**Base URL**: \`${doc.baseUrl}\``);
    console.log(`**Documentation**: [Reference](${doc.documentationLink})\n`);
    
    console.log(`## Managed Resources & Methods\n`);
    console.log(`Below are the resources exposed by this API and the REST methods supported:\n`);
    
    for (const r of resources) {
      console.log(`### Resource: \`${r.resource}\``);
      console.log(`* **Supported Methods**: ${r.methods.map(m => `\`${m}\``).join(', ')}`);
      console.log();
    }
  } catch (error) {
    console.error(`Error fetching details for API: ${error.message}`);
    process.exit(1);
  }
}

async function main() {
  const options = parseArgs();
  
  if (!options.list && !options.details) {
    printHelp();
    process.exit(1);
  }

  if (options.list) {
    await handleList(options);
  } else if (options.details) {
    await handleDetails(options);
  }
}

main();
