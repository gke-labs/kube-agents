#!/usr/bin/env node

/**
 * discover_kcc.cjs
 * KCC Supported Resource Discovery Tool for Antigravity Skill
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

const LOCAL_REPO_PATH = process.env.KCC_REPO_PATH;

function fetchUrl(url) {
  return new Promise((resolve, reject) => {
    const options = {
      headers: {
        'User-Agent': 'Antigravity-Skill-Agent',
        // GitHub API requires authorization for higher limits, but public rate limit is usually fine for directory listing.
      }
    };
    https.get(url, options, (res) => {
      if (res.statusCode === 403) {
        reject(new Error(`GitHub API rate limit exceeded or access forbidden (Status 403).`));
        return;
      }
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
    search: '',
    format: 'markdown'
  };

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--search') {
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
  console.log(`KCC Resource Discovery Script

Usage:
  node discover_kcc.cjs [--search <query>] [--format <markdown|json>]

Options:
  --search <query>     Filter KCC resources by name/service
  --format <format>    Output format: 'markdown' (default) or 'json'
`);
}

// Map plural file names to Kind approximations
function inferKind(plural, service) {
  // Simple heuristic mapping
  let base = plural.replace(service, '');
  if (base.endsWith('s')) {
    base = base.slice(0, -1);
  }
  
  // Uppercase first letters
  const capitalize = s => s.charAt(0).toUpperCase() + s.slice(1);
  
  const kindService = capitalize(service);
  const kindBase = base.split('-').map(capitalize).join('');
  return `${kindService}${kindBase}`;
}

async function getCRDListFromLocal() {
  const crdPath = path.join(LOCAL_REPO_PATH, 'config/crds/resources');
  if (!fs.existsSync(crdPath)) {
    return null;
  }
  const files = fs.readdirSync(crdPath);
  return files.map(file => ({ name: file }));
}

async function getCRDListFromGitHub() {
  const url = 'https://api.github.com/repos/GoogleCloudPlatform/k8s-config-connector/contents/config/crds/resources';
  const dataRaw = await fetchUrl(url);
  return JSON.parse(dataRaw);
}

async function main() {
  const options = parseArgs();

  try {
    let files = [];
    let isLocal = false;

    // Try local filesystem first
    const localFiles = await getCRDListFromLocal();
    if (localFiles) {
      files = localFiles;
      isLocal = true;
    } else {
      // Fallback to GitHub API
      files = await getCRDListFromGitHub();
    }

    // Filter and parse names
    let resources = [];
    const crdPrefix = 'apiextensions.k8s.io_v1_customresourcedefinition_';
    
    for (const file of files) {
      if (file.name.startsWith(crdPrefix) && file.name.endsWith('.yaml')) {
        const parts = file.name.slice(crdPrefix.length, -5).split('.');
        if (parts.length >= 2) {
          const pluralAndService = parts[0];
          const group = parts.slice(1).join('.');
          const service = parts[parts.length - 5] || parts[0]; // approximation
          
          // Parse the specific resource and service mapping from the filename
          // Example filename: ..._containerclusters.container.cnrm...yaml
          const match = parts[0].match(/^([a-z0-9\-]+)(?:\.([a-z0-9\-]+))?$/) || [parts[0], parts[0]];
          const resourceName = parts[0];
          const serviceName = parts[1] || 'cnrm';
          
          resources.push({
            filename: file.name,
            resource: resourceName,
            service: serviceName,
            group: `${serviceName}.cnrm.cloud.google.com`
          });
        }
      }
    }

    // Apply filter
    if (options.search) {
      const q = options.search.toLowerCase();
      resources = resources.filter(r => 
        r.resource.toLowerCase().includes(q) || 
        r.service.toLowerCase().includes(q)
      );
    }

    if (options.format === 'json') {
      console.log(JSON.stringify(resources, null, 2));
      return;
    }

    // Markdown Output
    console.log(`# Supported KCC Resources (${isLocal ? 'Local Source' : 'GitHub Source'})\n`);
    console.log(`Found ${resources.length} resources:\n`);
    console.log(`| Service/Group | Resource (Plural) | CRD Filename |`);
    console.log(`|---|---|---|`);
    for (const r of resources) {
      console.log(`| \`${r.service}\` | \`${r.resource}\` | [\`${r.filename.slice(0, 30)}...\`](https://github.com/GoogleCloudPlatform/k8s-config-connector/blob/master/config/crds/resources/${r.filename}) |`);
    }
  } catch (error) {
    console.error(`Error discovering KCC resources: ${error.message}`);
    process.exit(1);
  }
}

main();
