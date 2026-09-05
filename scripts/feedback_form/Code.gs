/**
 * kube-agents external feedback intake.
 *
 * A Google Form anyone can submit without a Google or GitHub account. Each
 * submission becomes an issue on gke-labs/kube-agents labelled
 * `external-feedback`. This exists because enterprise-managed GitHub accounts
 * cannot open issues on repositories outside their own enterprise, so people
 * at those companies had no way to reach the tracker. README.md next to this
 * file has the setup steps and the operating notes.
 *
 * Runs in Google Apps Script, not in this repository. Paste it into a new
 * project at script.google.com, run `setup` once, then give it the
 * kube-agents-bot App's id and a private key as script properties.
 */

const REPO = 'gke-labs/kube-agents';
const LABEL = 'external-feedback';
// Credentials. Issues are filed as the kube-agents-bot GitHub App: the script
// signs a short-lived JWT with the App's private key, exchanges it for an
// installation token limited to this repository and Issues: write, and uses
// that token once. A personal token in TOKEN_PROPERTY is the fallback when
// no App key is set.
const APP_ID_PROPERTY = 'GITHUB_APP_ID';
const APP_PRIVATE_KEY_PROPERTY = 'GITHUB_APP_PRIVATE_KEY';
const TOKEN_PROPERTY = 'GITHUB_TOKEN';
const APP_JWT_LIFETIME_SECONDS = 9 * 60; // GitHub's ceiling is ten minutes
const APP_JWT_BACKDATE_SECONDS = 60; // tolerates clock skew against GitHub
const PEM_HEADER = '-----BEGIN';
// GitHub downloads App keys as PKCS#1 ("BEGIN RSA PRIVATE KEY"). Apps Script's
// signer wants PKCS#8 ("BEGIN PRIVATE KEY"), which is the same key inside a
// DER wrapper naming the rsaEncryption algorithm. The script does the wrapping.
const PKCS1_LABEL = 'RSA PRIVATE KEY';
const PKCS8_LABEL = 'PRIVATE KEY';
const PEM_LINE_LENGTH = 64;
const DER_SEQUENCE = 0x30;
const DER_OCTET_STRING = 0x04;
// version INTEGER 0, then AlgorithmIdentifier { OID 1.2.840.113549.1.1.1, NULL }
const PKCS8_RSA_PREAMBLE = [
  0x02, 0x01, 0x00, 0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00,
];
const FORM_ID_PROPERTY = 'FORM_ID';
// Apps Script can fire a submit trigger twice for one response. A response id
// seen within this window is not filed again.
const FILED_CACHE_PREFIX = 'filed:';
const FILED_CACHE_SECONDS = 6 * 60 * 60;
const LOCK_WAIT_MS = 30 * 1000;
const SUBMIT_HANDLER = 'onFormSubmit';
const FORM_TITLE = 'kube-agents feedback';
const FORM_DESCRIPTION =
  'Bug reports, feature requests, and questions for kube-agents ' +
  '(github.com/gke-labs/kube-agents). Everything you enter here, except the ' +
  'contact email, is posted as a public GitHub issue.';
const CONFIRMATION_MESSAGE =
  'Thanks. Your report is being filed as a public issue on ' +
  'github.com/gke-labs/kube-agents/issues and should appear there within a minute.';
const API_BASE_URL = 'https://api.github.com';
const ISSUES_API_URL = API_BASE_URL + '/repos/' + REPO + '/issues';
const REPO_INSTALLATION_URL = API_BASE_URL + '/repos/' + REPO + '/installation';
const INSTALLATIONS_URL = API_BASE_URL + '/app/installations/';
const GITHUB_API_VERSION = '2022-11-28';
const HTTP_OK = 200;
const HTTP_CREATED = 201;
const TITLE_MAX_CHARS = 120;
const NOT_GIVEN = 'not given';
const DEFAULT_TITLE = 'Feedback from the form';

// Form questions in display order. `key` is how onFormSubmit reads an answer,
// `title` is what the responder sees and is also the match key, so a title
// edited in the form UI must be edited here too.
const QUESTIONS = [
  {
    key: 'title',
    title: 'One-line summary',
    type: 'text',
    required: true,
    help: 'Becomes the issue title.',
  },
  {
    key: 'kind',
    title: 'What kind of feedback is this?',
    type: 'choice',
    required: true,
    choices: ['Bug', 'Feature request', 'Question', 'Other'],
  },
  {
    key: 'happened',
    title: 'What happened?',
    type: 'paragraph',
    required: true,
    help: 'What you did and what you saw. For a bug, the steps to reproduce it.',
  },
  {
    key: 'expected',
    title: 'What did you expect instead?',
    type: 'paragraph',
    required: false,
  },
  {
    key: 'environment',
    title: 'Version and environment',
    type: 'text',
    required: false,
    help: 'Chart or image tag, GKE version, model provider. Whatever you know.',
  },
  {
    key: 'extra',
    title: 'Logs, links, or anything else',
    type: 'paragraph',
    required: false,
  },
  {
    key: 'name',
    title: 'Your name or handle',
    type: 'text',
    required: false,
    help: 'Goes in the issue so we can credit you. Leave blank to stay anonymous.',
  },
  {
    key: 'contact',
    title: 'Email for follow-up',
    type: 'text',
    required: false,
    help: 'Never posted to GitHub. Kept only in the form responses, visible to the form owner.',
  },
];

// Which extra label the "kind" answer adds. Unlisted kinds add none.
const KIND_LABELS = {
  Bug: 'bug',
  'Feature request': 'enhancement',
  Question: 'question',
};

// Sections of the issue body, in order: which answer, and its heading.
const BODY_SECTIONS = [
  { key: 'happened', heading: 'What happened' },
  { key: 'expected', heading: 'What was expected' },
  { key: 'environment', heading: 'Version and environment' },
  { key: 'extra', heading: 'Logs, links, or anything else' },
];

/**
 * Setup: creates the form if none is recorded, then brings it to the
 * configured state. Every step is safe to repeat, so a run that threw
 * partway (the external-sharing call is the likely one) is finished by
 * running it again: the form id is recorded the moment the form exists,
 * settings are re-applied, questions are added only to an empty form, and
 * the submit trigger is created only if this form has none.
 */
function setup() {
  const props = PropertiesService.getScriptProperties();
  let form;
  const existing = props.getProperty(FORM_ID_PROPERTY);
  if (existing) {
    try {
      form = FormApp.openById(existing);
    } catch (err) {
      throw new Error(
        'Script property ' + FORM_ID_PROPERTY + ' names form ' + existing + ', which cannot be opened (' +
          err + '). If that form was deleted, remove the property and run setup again.'
      );
    }
  } else {
    form = FormApp.create(FORM_TITLE);
    props.setProperty(FORM_ID_PROPERTY, form.getId());
  }

  form.setDescription(FORM_DESCRIPTION);
  form.setConfirmationMessage(CONFIRMATION_MESSAGE);
  // Anyone with the link, no sign-in. This is the whole point: the people this
  // form serves cannot use their work identity here. Throws if the Workspace
  // domain forbids external forms; see README.md.
  form.setRequireLogin(false);
  form.setCollectEmail(false);
  form.setLimitOneResponsePerUser(false);
  form.setAllowResponseEdits(false);

  if (form.getItems().length === 0) {
    QUESTIONS.forEach(function (q) {
      addQuestion(form, q);
    });
  } else {
    Logger.log('Form already has questions; leaving them as they are.');
  }

  if (!hasSubmitTrigger(form)) {
    ScriptApp.newTrigger(SUBMIT_HANDLER).forForm(form).onFormSubmit().create();
  }

  Logger.log('Share this link: %s', form.getPublishedUrl());
  Logger.log('Edit the form here: %s', form.getEditUrl());
  if (!props.getProperty(APP_PRIVATE_KEY_PROPERTY) && !props.getProperty(TOKEN_PROPERTY)) {
    Logger.log(
      'Now set the %s and %s script properties (Project Settings > Script Properties).',
      APP_ID_PROPERTY,
      APP_PRIVATE_KEY_PROPERTY
    );
  }
}

function hasSubmitTrigger(form) {
  return ScriptApp.getProjectTriggers().some(function (t) {
    return t.getTriggerSourceId() === form.getId() && t.getHandlerFunction() === SUBMIT_HANDLER;
  });
}

function addQuestion(form, q) {
  let item;
  if (q.type === 'paragraph') {
    item = form.addParagraphTextItem();
  } else if (q.type === 'choice') {
    item = form.addMultipleChoiceItem();
    item.setChoiceValues(q.choices);
  } else {
    item = form.addTextItem();
  }
  item.setTitle(q.title).setRequired(q.required);
  if (q.help) {
    item.setHelpText(q.help);
  }
}

/**
 * Installed trigger: files the submission as a GitHub issue. On failure it
 * emails the form owner the full issue so nothing is lost, then rethrows so
 * the failure also shows in the Apps Script executions log. The submission
 * itself stays in the form's responses either way. A failure to send the
 * email is logged and does not replace the GitHub error being rethrown.
 * A response id seen recently is skipped, because the platform sometimes
 * fires this trigger twice for one submission.
 */
function onFormSubmit(e) {
  if (!claimResponse(e.response.getId())) {
    Logger.log('Response %s already handled; skipping duplicate trigger.', e.response.getId());
    return;
  }

  const answers = answersByKey(e.response);
  const issue = buildIssue(answers, e.source.getPublishedUrl());
  try {
    const url = createIssue(issue);
    Logger.log('Filed %s', url);
  } catch (err) {
    try {
      notifyOwner(issue, err);
    } catch (mailErr) {
      Logger.log('Could not email the owner about the failure below: %s', mailErr);
    }
    throw err;
  }
}

/**
 * Marks a response id as handled and says whether this execution was the
 * one to do it. The check and the mark happen under the script lock, so two
 * executions that start together for the same response cannot both win.
 */
function claimResponse(responseId) {
  const lock = LockService.getScriptLock();
  lock.waitLock(LOCK_WAIT_MS);
  try {
    const cache = CacheService.getScriptCache();
    const seenKey = FILED_CACHE_PREFIX + responseId;
    if (cache.get(seenKey)) {
      return false;
    }
    cache.put(seenKey, '1', FILED_CACHE_SECONDS);
    return true;
  } finally {
    lock.releaseLock();
  }
}

function answersByKey(response) {
  const keyByTitle = {};
  QUESTIONS.forEach(function (q) {
    keyByTitle[q.title] = q.key;
  });
  const answers = {};
  response.getItemResponses().forEach(function (r) {
    const key = keyByTitle[r.getItem().getTitle()];
    if (key) {
      answers[key] = String(r.getResponse() || '').trim();
    }
  });
  return answers;
}

function buildIssue(answers, formUrl) {
  // Truncate on code points, not UTF-16 units, so a 120-unit cut cannot
  // split an emoji into a lone surrogate. A summary longer than the title
  // allows is kept whole in the body, so nothing the reporter wrote is lost.
  const summary = Array.from(answers.title || DEFAULT_TITLE);
  const title = summary.slice(0, TITLE_MAX_CHARS).join('');
  const overflow = summary.length > TITLE_MAX_CHARS;
  const reporter = answers.name || NOT_GIVEN;
  const labels = [LABEL];
  if (Object.prototype.hasOwnProperty.call(KIND_LABELS, answers.kind)) {
    labels.push(KIND_LABELS[answers.kind]);
  }

  // Plain text, not italics: a reporter name containing `_` would otherwise
  // end the emphasis early.
  const lines = [
    'Filed from the [feedback form](' + formUrl + ') on behalf of an outside reporter. ' +
      'Reporter: ' + reporter + '. Contact details, if given, are in the form responses, not here.',
    '',
    '**Kind:** ' + (answers.kind || NOT_GIVEN),
  ];
  if (overflow) {
    lines.push('', '## Summary', '', summary.join(''));
  }
  BODY_SECTIONS.forEach(function (s) {
    const text = answers[s.key];
    if (text) {
      lines.push('', '## ' + s.heading, '', text);
    }
  });

  return { title: title, body: lines.join('\n'), labels: labels };
}

function createIssue(issue) {
  return githubJson('post', ISSUES_API_URL, authToken(), issue, HTTP_CREATED).html_url;
}

/**
 * The bearer token to file with: a fresh installation token for the App when
 * its id and key are configured, otherwise the personal token.
 */
function authToken() {
  const props = PropertiesService.getScriptProperties();
  const appId = props.getProperty(APP_ID_PROPERTY);
  const pem = props.getProperty(APP_PRIVATE_KEY_PROPERTY);
  if (appId && pem) {
    return installationToken(appId, normalisePem(pem));
  }
  const token = props.getProperty(TOKEN_PROPERTY);
  if (token) {
    return token;
  }
  throw new Error(
    'No credentials: set script properties ' + APP_ID_PROPERTY + ' and ' + APP_PRIVATE_KEY_PROPERTY +
      ' (or ' + TOKEN_PROPERTY + '). See README.md.'
  );
}

/**
 * Accepts the key as pasted into the properties UI (real newlines), as it
 * often arrives through a shell (literal backslash-n), or as one base64 line,
 * in either PKCS#1 or PKCS#8, and returns a PKCS#8 PEM the signer accepts.
 */
function normalisePem(value) {
  let pem = value.replace(/\\n/g, '\n').trim();
  if (pem.indexOf(PEM_HEADER) !== 0) {
    pem = Utilities.newBlob(Utilities.base64Decode(pem)).getDataAsString().trim();
  }
  const parsed = parsePem(pem);
  if (parsed.label === PKCS8_LABEL) {
    return pem;
  }
  if (parsed.label === PKCS1_LABEL) {
    return toPem(PKCS8_LABEL, wrapPkcs1AsPkcs8(parsed.der));
  }
  throw new Error('Unsupported key type "' + parsed.label + '"; expected an RSA private key.');
}

function parsePem(pem) {
  const m = pem.match(/-----BEGIN ([^-]+)-----([\s\S]*?)-----END \1-----/);
  if (!m) {
    throw new Error('Script property ' + APP_PRIVATE_KEY_PROPERTY + ' is not a PEM block.');
  }
  return { label: m[1], der: Utilities.base64Decode(m[2].replace(/\s+/g, '')) };
}

function toPem(label, der) {
  const b64 = Utilities.base64Encode(der);
  const lines = [];
  for (let i = 0; i < b64.length; i += PEM_LINE_LENGTH) {
    lines.push(b64.slice(i, i + PEM_LINE_LENGTH));
  }
  return '-----BEGIN ' + label + '-----\n' + lines.join('\n') + '\n-----END ' + label + '-----';
}

function wrapPkcs1AsPkcs8(pkcs1) {
  const inner = PKCS8_RSA_PREAMBLE.concat([DER_OCTET_STRING], derLength(pkcs1.length), pkcs1);
  return [DER_SEQUENCE].concat(derLength(inner.length), inner);
}

function derLength(n) {
  if (n < 0x80) {
    return [n];
  }
  const bytes = [];
  for (let v = n; v > 0; v = Math.floor(v / 0x100)) {
    bytes.unshift(v % 0x100);
  }
  return [0x80 | bytes.length].concat(bytes);
}

/**
 * Exchanges an App JWT for an installation token that can write issues on
 * this repository and nothing else. Minted per submission; volume is low and
 * a token is good for an hour, so there is nothing worth caching.
 */
function installationToken(appId, pem) {
  const jwt = appJwt(appId, pem);
  const installation = githubJson('get', REPO_INSTALLATION_URL, jwt, null, HTTP_OK);
  const repoName = REPO.split('/')[1];
  const grant = githubJson(
    'post',
    INSTALLATIONS_URL + installation.id + '/access_tokens',
    jwt,
    { repositories: [repoName], permissions: { issues: 'write' } },
    HTTP_CREATED
  );
  return grant.token;
}

function appJwt(appId, pem) {
  const now = Math.floor(Date.now() / 1000);
  const header = base64Url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const payload = base64Url(
    JSON.stringify({ iss: appId, iat: now - APP_JWT_BACKDATE_SECONDS, exp: now + APP_JWT_LIFETIME_SECONDS })
  );
  const signature = Utilities.computeRsaSha256Signature(header + '.' + payload, pem);
  return header + '.' + payload + '.' + base64Url(signature);
}

function base64Url(value) {
  return Utilities.base64EncodeWebSafe(value).replace(/=+$/, '');
}

function githubJson(method, url, bearer, payload, expectedCode) {
  const options = {
    method: method,
    headers: {
      Authorization: 'Bearer ' + bearer,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': GITHUB_API_VERSION,
    },
    muteHttpExceptions: true,
  };
  if (payload) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(payload);
  }
  const res = UrlFetchApp.fetch(url, options);
  if (res.getResponseCode() !== expectedCode) {
    throw new Error(
      'GitHub returned ' + res.getResponseCode() + ' for ' + method.toUpperCase() + ' ' + url + ': ' + res.getContentText()
    );
  }
  return JSON.parse(res.getContentText());
}

function notifyOwner(issue, err) {
  const to = Session.getEffectiveUser().getEmail();
  const body =
    'The feedback form could not file this issue on ' + REPO + '.\n\n' +
    'Error: ' + err + '\n\n' +
    'File it by hand:\n\n' +
    'Title: ' + issue.title + '\n' +
    'Labels: ' + issue.labels.join(', ') + '\n\n' +
    issue.body;
  MailApp.sendEmail(to, '[kube-agents feedback form] failed to file: ' + issue.title, body);
}
