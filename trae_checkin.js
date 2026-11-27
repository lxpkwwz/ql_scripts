/*
 * Trae Work 每日签到 · 青龙单文件版
 *
 * 功能：自动续期 Token、每日签到、查询剩余积分、合并每日签到余额、
 *       企业微信/青龙通知、每日状态去重、限流冷却、多账号错峰。
 *
 * 推荐定时任务：
 *   node /ql/data/scripts/trae_checkin_merged.js
 *
 * 【推荐配置：持久认证】
 * 单账号：
 *   TRAE_ACCOUNTS=[{"name":"账号1","uid":"account1","refreshToken":"...","deviceId":"..."}]
 *
 * 多账号：
 *   TRAE_ACCOUNTS=[
 *     {"name":"账号1","uid":"account1","refreshToken":"...","deviceId":"..."},
 *     {"name":"账号2","uid":"account2","refreshToken":"...","deviceId":"..."}
 *   ]
 *
 * 字段说明：
 *   name          通知中显示的名称
 *   uid           稳定且唯一的账号标识，用于区分缓存和状态
 *   refreshToken  从 Trae 客户端/网页登录流程获取的可续期凭据
 *   deviceId      当前成功签到请求中的 x-device-id，可选但 9004 时建议填写
 *   headers       可放 x-market-client-id、x-user-region、package-type、app-version、User-Agent 等
 *
 * 注意：TRAE_ACCOUNTS 中的 JSON 必须在青龙环境变量里写成一行。
 * 不要把旧的 authorization、完整 Cookie 或 Reqable 动态签名头长期写入配置。
 * 脚本会自动续期并保存最新 Token。
 *
 * 【单账号简化配置】
 *   TRAE_REFRESH_TOKEN=...
 *   TRAE_UID=账号1
 *   TRAE_DEVICE_ID=真实 x-device-id（可选）
 *
 * 【认证缓存】
 *   TRAE_TOKEN_CACHE=/ql/data/config/trae-token-cache.json
 * 默认使用上面的路径，保存自动轮换后的 accessToken/refreshToken。
 * 不要删除，也不要让多个任务同时刷新同一账号。
 *
 * 【签到与余额接口】
 *   TRAE_API_HOST       默认 https://api.trae.cn
 *   TRAE_STATUS_PATH    默认 /trae/api/v2/ug/checkin_credits/status
 *   TRAE_CLAIM_PATH     默认 /trae/api/v2/ug/checkin_credits/claim
 *   TRAE_CLAIM_BODY     默认 `{}`；如果真实成功请求体不同，可覆盖
 *   TRAE_USAGE_PATH     默认 /trae/api/v2/pay/ide_user_ent_usage
 *   TRAE_BALANCE        默认 1；设为 0 关闭余额查询
 *
 * 【运行控制】
 *   TRAE_STATE_FILE     默认 /ql/data/config/trae-checkin-state.json
 *   TRAE_SKIP_DONE      默认 1；设为 0 不跳过当天已处理账号
 *   TRAE_JITTER         默认随机等待 0~20 秒；设为 0 关闭
 *   TRAE_GAP_MIN/MAX    多账号间隔，默认 12/25 秒
 *   TRAE_COOLDOWN_MIN   9074/429 限流后的账号冷却时间，默认 55 分钟
 *   TRAE_NOTIFY         默认 1；设为 0 关闭通知
 *   TRAE_DEBUG          默认 0；设为 1 输出接口诊断，不输出请求头
 *
 * 【旧配置兼容】
 *   TRAE_AUTHORIZATION、TRAE_COOKIE、TRAE_HEADERS、TRAE_RAW_HEADERS、TRAE_TOKEN
 * 仍可使用，但这些抓包凭据会过期，优先改用 TRAE_REFRESH_TOKEN。
 */

'use strict';


// ================= 持久认证：refreshToken 自动续期 =================
const fs = require('node:fs');
const TOKEN_CACHE_FILE = process.env.TRAE_TOKEN_CACHE || '/ql/data/config/trae-token-cache.json';
const REFRESH_TIMEOUT = Number(process.env.TRAE_REFRESH_TIMEOUT || 15000);
const REFRESH_CLIENT_ID = process.env.TRAE_CLIENT_ID || 'en1oxy7wnw8j9n';
const REFRESH_URLS = [
  process.env.TRAE_EXCHANGE_URL || 'https://api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken',
  'https://api.trae.cn/trae/api/v3/oauth/ExchangeToken',
];
const STATE_FILE = process.env.TRAE_STATE_FILE || '/ql/data/config/trae-checkin-state.json';
const SKIP_DONE = process.env.TRAE_SKIP_DONE !== '0';
const COOLDOWN_MIN = Math.max(1, Number(process.env.TRAE_COOLDOWN_MIN || 55));
const GAP_MIN = Math.max(0, Number(process.env.TRAE_GAP_MIN || 12));
const GAP_MAX = Math.max(GAP_MIN, Number(process.env.TRAE_GAP_MAX || 25));

function persistentReadJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}

function persistentWriteJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(value, null, 2), { mode: 0o600 });
  fs.renameSync(temp, file);
}

function persistentJwtExp(token) {
  try {
    const part = String(token).split('.')[1];
    const payload = JSON.parse(Buffer.from(part, 'base64url').toString('utf8'));
    return Number(payload.exp || 0);
  } catch { return 0; }
}

function persistentTokenUsable(token, margin = 120) {
  const exp = persistentJwtExp(token);
  return Boolean(token && exp && exp > Math.floor(Date.now() / 1000) + margin);
}

function readRunState() {
  const state = persistentReadJson(STATE_FILE, {});
  return state && typeof state === 'object' ? state : {};
}

function writeRunState(state) {
  try { persistentWriteJson(STATE_FILE, state); }
  catch (error) { console.log(`签到状态写入失败：${error.message || error}`); }
}

function accountStateKey(account, index) {
  return String(account.uid || account.name || index + 1);
}

function beijingDate() {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function isRateLimited(error) {
  const message = String(error?.message || error || '');
  return Number(error?.status) === 429
    || /\b(429|9074)\b/.test(message)
    || /rate.?limit|too many|参与用户过多|请求过于频繁/i.test(message);
}

function markCooldown(state, key, reason) {
  state[key] = { ...(state[key] || {}), cooldownUntil: Date.now() + COOLDOWN_MIN * 60 * 1000, reason };
}

function isCoolingDown(state, key) {
  return Number(state[key]?.cooldownUntil || 0) > Date.now();
}

function persistentAccounts() {
  if (process.env.TRAE_ACCOUNTS) {
    const list = JSON.parse(process.env.TRAE_ACCOUNTS);
    if (!Array.isArray(list) || !list.length) throw new Error('TRAE_ACCOUNTS 必须是非空 JSON 数组');
    return list.map((account, index) => ({
      ...account,
      name: account.name || account.uid || `账号${index + 1}`,
      uid: account.uid || account.name || String(index + 1),
      deviceId: account.deviceId || account.realDeviceId || '',
    }));
  }
  return [{
    name: process.env.TRAE_UID || '账号1',
    uid: process.env.TRAE_UID || '1',
    accessToken: process.env.TRAE_ACCESS_TOKEN || '',
    refreshToken: process.env.TRAE_REFRESH_TOKEN || '',
    deviceId: process.env.TRAE_DEVICE_ID || '',
    headers: {},
  }];
}

async function persistentPostJson(url, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REFRESH_TIMEOUT);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        accept: 'application/json',
        // refresh-token 分支要求为空，不能放旧 access token。
        'x-cloudide-token': '',
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch {
      throw new Error(`续期响应不是 JSON（HTTP ${response.status}）`);
    }
    if (!response.ok) {
      const message = data?.ResponseMetadata?.Error?.Message || data?.message || data?.msg || '';
      throw new Error(`HTTP ${response.status}${message ? `：${message}` : ''}`);
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

function persistentResult(data) {
  return data?.Result || data?.result || data?.data || data || {};
}

async function persistentRefresh(refreshToken) {
  let lastError;
  for (const url of REFRESH_URLS) {
    try {
      // 适用于网页 OAuth refreshToken；不伪造设备签名。
      const data = await persistentPostJson(url, {
        ClientID: REFRESH_CLIENT_ID,
        RefreshToken: refreshToken,
        ClientSecret: '-',
        UserID: '',
      });
      const result = persistentResult(data);
      const accessToken = result.Token || result.token || result.AccessToken || result.accessToken;
      const rotatedRefreshToken = result.RefreshToken || result.refreshToken || refreshToken;
      if (!accessToken) throw new Error('续期响应中没有新的 accessToken');
      return { accessToken, refreshToken: rotatedRefreshToken };
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error('没有可用的续期接口');
}

async function preparePersistentAuth() {
  const accounts = persistentAccounts();
  const cache = persistentReadJson(TOKEN_CACHE_FILE, {});
  // 没有任何持久认证配置时，不接管原脚本的旧环境变量回退链。
  if (!process.env.TRAE_ACCOUNTS
    && !process.env.TRAE_REFRESH_TOKEN
    && !process.env.TRAE_ACCESS_TOKEN
    && Object.keys(cache).length === 0) {
    return;
  }
  let cacheChanged = false;

  for (let index = 0; index < accounts.length; index += 1) {
    const account = accounts[index];
    const key = String(account.uid || account.name || index + 1);
    const old = cache[key] || {};
    let accessToken = account.accessToken || old.accessToken || '';
    let refreshToken = account.refreshToken || old.refreshToken || '';

    if (refreshToken && !persistentTokenUsable(accessToken)) {
      try {
        const fresh = await persistentRefresh(refreshToken);
        accessToken = fresh.accessToken;
        refreshToken = fresh.refreshToken || refreshToken;
        console.log(`${account.name}：已自动续期认证 Token`);
        cacheChanged = true;
      } catch (error) {
        console.log(`${account.name}：自动续期失败，回退旧认证方式（${error.message || error}）`);
      }
    }

    if (accessToken) {
      const headers = { ...(account.headers || {}) };
      delete headers.authorization;
      delete headers.Authorization;
      if (account.deviceId && !headers['x-device-id'] && !headers['X-Device-Id']) {
        headers['x-device-id'] = String(account.deviceId);
      }
      account.headers = { ...headers, Authorization: `Cloud-IDE-JWT ${accessToken}` };
    }
    if (accessToken || refreshToken) {
      cache[key] = {
        accessToken,
        refreshToken,
        expiresAt: persistentJwtExp(accessToken),
        updatedAt: Date.now(),
      };
      cacheChanged = true;
    }
  }

  if (cacheChanged) {
    try {
      persistentWriteJson(TOKEN_CACHE_FILE, cache);
    } catch (error) {
      console.log(`认证缓存写入失败：${error.message || error}`);
    }
  }

  // 让原有 buildAccounts 使用经过续期的多账号数据。
  process.env.TRAE_ACCOUNTS = JSON.stringify(accounts);
  delete process.env.TRAE_AUTHORIZATION;
  delete process.env.TRAE_TOKEN;
}

const path = require('node:path');
const { spawnSync } = require('node:child_process');
const DEFAULT_API_HOST = 'https://api.trae.cn';
const STATUS_PATH = process.env.TRAE_STATUS_PATH || '/trae/api/v2/ug/checkin_credits/status';
const CLAIM_PATH = process.env.TRAE_CLAIM_PATH || '/trae/api/v2/ug/checkin_credits/claim';
const USAGE_PATH = process.env.TRAE_USAGE_PATH || '/trae/api/v2/pay/ide_user_ent_usage';
const BALANCE_ENABLED = process.env.TRAE_BALANCE !== '0';
const TIMEOUT = Number(process.env.TRAE_TIMEOUT || 15000);
const DEBUG = process.env.TRAE_DEBUG === '1';

function normalizeHost(host) {
  return String(host || '').trim().replace(/\/+$/, '');
}

function buildApiHosts() {
  const configured = process.env.TRAE_API_HOSTS || process.env.TRAE_API_HOST || '';
  const candidates = configured
    .split(/[\n,]+/)
    .map(normalizeHost)
    .filter(Boolean);

  // 兼容旧配置：如果青龙里仍保存 api.trae.com.cn，遇到网关 404 时自动尝试当前中国区主机。
  candidates.push(DEFAULT_API_HOST);
  return [...new Set(candidates)];
}

const API_HOSTS = buildApiHosts();

function parseJsonEnv(name, fallback = undefined) {
  const raw = process.env[name];
  if (!raw || !raw.trim()) return fallback;
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`${name} 不是有效的 JSON：${error.message}`);
  }
}

function buildAccounts() {
  const accounts = parseJsonEnv('TRAE_ACCOUNTS');
  if (accounts !== undefined) {
    if (!Array.isArray(accounts) || accounts.length === 0) {
      throw new Error('TRAE_ACCOUNTS 必须是非空 JSON 数组');
    }
    return accounts.map((account, index) => ({
      name: account.name || `账号${index + 1}`,
      headers: account.headers || {},
    }));
  }

  const headers = parseJsonEnv('TRAE_HEADERS', {});
  if (!headers || typeof headers !== 'object' || Array.isArray(headers)) {
    throw new Error('TRAE_HEADERS 必须是 JSON 对象');
  }

  if (process.env.TRAE_COOKIE && !headers.Cookie && !headers.cookie) {
    headers.Cookie = process.env.TRAE_COOKIE;
  }
  Object.assign(headers, parseRawHeadersEnv('TRAE_RAW_HEADERS'));
  if (process.env.TRAE_AUTHORIZATION && process.env.TRAE_AUTHORIZATION.trim()) {
    delete headers.authorization;
    headers.Authorization = process.env.TRAE_AUTHORIZATION.trim();
  }
  if (process.env.TRAE_TOKEN && !headers.Authorization && !headers.authorization) {
    const token = process.env.TRAE_TOKEN.trim();
    headers.Authorization = /^Bearer\s+/i.test(token) ? token : `Bearer ${token}`;
  }

  return [{ name: '账号1', headers }];
}

function normalizeHeaders(headers) {
  const result = {
    Accept: 'application/json, text/plain, */*',
    'Content-Type': 'application/json',
    Origin: 'https://work.trae.cn',
    Referer: 'https://work.trae.cn/',
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36',
  };

  for (const [key, value] of Object.entries(headers || {})) {
    if (value !== undefined && value !== null && String(value).trim()) {
      result[key] = String(value);
    }
  }
  return result;
}

class HttpError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'HttpError';
    Object.assign(this, details);
  }
}

function parseRawHeadersEnv(name) {
  const raw = process.env[name];
  if (!raw || !raw.trim()) return {};
  const skip = new Set(['host', 'content-length', 'accept-encoding', 'connection', 'transfer-encoding']);
  const headers = {};
  for (const line of raw.split(/\r?\n/)) {
    const separator = line.indexOf(':');
    if (separator <= 0) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (!key || !value || skip.has(key.toLowerCase())) continue;
    headers[key] = value;
  }
  return headers;
}

function parseOptionalBody(name) {
  const raw = process.env[name];
  if (!raw || !raw.trim()) return undefined;
  try {
    return JSON.stringify(JSON.parse(raw));
  } catch {
    // 兼容少数非 JSON 请求体；不要在这里猜测或改写用户复制的内容。
    return raw;
  }
}

async function requestJson(url, headers, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT);

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers,
      ...(body === undefined ? {} : { body }),
      credentials: 'include',
      signal: controller.signal,
    });

    const contentType = response.headers.get('content-type') || '';
    const text = await response.text();
    let data = {};

    if (text.trim()) {
      try {
        data = JSON.parse(text);
      } catch {
        const preview = text.replace(/\s+/g, ' ').slice(0, 220);
        throw new HttpError(
          `HTTP ${response.status}，响应不是 JSON：${preview}`,
          { status: response.status, url, contentType, body: text },
        );
      }
    }

    if (!response.ok) {
      const message = data?.message || data?.msg || `HTTP ${response.status}`;
      throw new HttpError(`${message}（HTTP ${response.status}）`, {
        status: response.status,
        url,
        contentType,
        body: text,
        data,
      });
    }

    return data;
  } catch (error) {
    if (error.name === 'AbortError') {
      throw new HttpError(`请求超时（${TIMEOUT} ms）`, { url, cause: error });
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function unwrap(data) {
  if (data && data.data && typeof data.data === 'object') return data.data;
  return data || {};
}

function getErrorMessage(data) {
  const value = unwrap(data);
  return value.message || value.msg || data?.message || data?.msg || '未知错误';
}

function isHtml404(error) {
  if (!error || Number(error.status) !== 404) return false;
  const type = String(error.contentType || '').toLowerCase();
  const body = String(error.body || '').toLowerCase();
  return type.includes('text/html') || body.includes('<html') || body.includes('<!doctype');
}

async function requestStatus(headers) {
  let lastError;

  for (const host of API_HOSTS) {
    const url = `${host}${STATUS_PATH}`;
    if (DEBUG) console.log(`诊断：尝试状态接口 ${url}`);
    try {
      const data = await requestJson(url, headers);
      return { host, data: unwrap(data) };
    } catch (error) {
      lastError = error;
      // 旧域名通常返回网关 HTML 404；只在这种情况下切换候选主机，避免重复请求领取接口。
      if (!isHtml404(error)) throw error;
      if (DEBUG) console.log(`诊断：${host} 返回 HTML 404，尝试下一个 API 主机`);
    }
  }

  throw lastError || new Error('没有可用的 Trae API 主机');
}

function toFiniteNumber(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return null;
}

function formatCredits(value) {
  const number = toFiniteNumber(value);
  if (number === null) return String(value ?? '未知');
  return Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

function dateFromValue(value) {
  if (value === undefined || value === null || value === '') return null;
  const numeric = toFiniteNumber(value);
  const dateValue = numeric === null ? new Date(value) : new Date(numeric < 1e12 ? numeric * 1000 : numeric);
  return Number.isNaN(dateValue.getTime()) ? null : dateValue;
}

function formatExpiry(value) {
  const dateValue = dateFromValue(value);
  if (!dateValue) return value === undefined || value === null || value === '' ? '' : String(value);
  return dateValue.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

function formatExpiryDate(value) {
  const dateValue = dateFromValue(value);
  if (!dateValue) return '';
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric',
  }).formatToParts(dateValue);
  const values = Object.fromEntries(parts.filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
  return `${values.year}/${values.month}/${values.day}`;
}

function firstValue(object, keys) {
  if (!object || typeof object !== 'object') return undefined;
  for (const key of keys) {
    if (object[key] !== undefined && object[key] !== null && object[key] !== '') return object[key];
  }
  return undefined;
}

function extractDetailLimit(object) {
  const quota = object?.quota || object?.entitlement_quota || {};
  return toFiniteNumber(firstValue(object, ['credits_limit', 'credit_limit', 'quota_amount', 'total_credits', 'total_amount', 'amount'])
    ?? firstValue(quota, ['credits_limit', 'credit_limit', 'amount', 'total']));
}

function extractDetailUsed(object) {
  const usage = object?.usage || {};
  return toFiniteNumber(firstValue(object, ['credits_amount', 'used_credits', 'used_amount', 'used'])
    ?? firstValue(usage, ['credits_amount', 'used_credits', 'used_amount', 'used'])) ?? 0;
}

function extractDetailTime(object, kind) {
  const keys = kind === 'grant'
    ? ['grant_time', 'grantTime', 'issue_time', 'issueTime', 'reward_time', 'rewardTime', 'start_time', 'startTime', 'create_time', 'created_at', 'date']
    : ['end_time', 'endTime', 'expire_time', 'expireTime', 'expired_at', 'expire_date'];
  return firstValue(object, keys);
}

function collectDailyDetailObjects(node, output, depth = 0) {
  if (!node || typeof node !== 'object' || depth > 8) return;
  if (Array.isArray(node)) {
    for (const item of node) {
      if (!item || typeof item !== 'object') continue;
      const limit = extractDetailLimit(item);
      const grantTime = extractDetailTime(item, 'grant');
      const expire = extractDetailTime(item, 'expire');
      if (limit !== null && (grantTime || expire)) output.push({ limit, used: extractDetailUsed(item), grantTime, expire });
      collectDailyDetailObjects(item, output, depth + 1);
    }
    return;
  }
  for (const value of Object.values(node)) {
    if (value && typeof value === 'object') collectDailyDetailObjects(value, output, depth + 1);
  }
}

function extractDailyDetailRows(pack, base) {
  const displayName = String(pack?.display_desc || '');
  if (!/每日签到|签到奖励/.test(displayName)) return [];
  const details = [];
  collectDailyDetailObjects(pack, details);
  const unique = new Map();
  for (const detail of details) {
    const key = JSON.stringify([detail.limit, detail.used, detail.grantTime, detail.expire]);
    if (!unique.has(key)) unique.set(key, detail);
  }
  return [...unique.values()].map(detail => ({
    name: '每日签到',
    limit: detail.limit,
    used: detail.used,
    expire: detail.expire || base.end_time,
  }));
}

function isDailyCreditRow(row) {
  return /每日签到|签到奖励/.test(String(row.name || ''));
}

function isDisplayedCreditRow(row) {
  return isDailyCreditRow(row) || /每月登录赠送/.test(String(row.name || ''));
}

function compactUsageRows(rows) {
  const compacted = [];
  let daily;
  for (const row of rows) {
    const remain = row.limit === null ? null : row.limit - row.used;
    if (remain === null || remain <= 0) continue;
    if (!isDisplayedCreditRow(row)) continue;
    if (!isDailyCreditRow(row)) {
      compacted.push(row);
      continue;
    }
    if (!daily) {
      daily = { ...row, name: '每日签到', limit: 0, used: 0, expiries: [] };
      compacted.push(daily);
    }
    daily.limit += row.limit ?? 0;
    daily.used += row.used ?? 0;
    if (row.expire) daily.expiries.push(row.expire);
  }
  return compacted;
}

function dailyExpiryText(row) {
  const dates = (row.expiries || []).map(dateFromValue).filter(Boolean).sort((a, b) => a - b);
  if (dates.length === 0) return '';
  const first = formatExpiryDate(dates[0]);
  const last = formatExpiryDate(dates[dates.length - 1]);
  return first === last ? `，到期 ${first}` : `，到期 ${first}至 ${last}`;
}

function monthlyLoginLabel() {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: 'numeric',
  }).formatToParts(new Date());
  const month = parts.find(part => part.type === 'month')?.value || String(new Date().getMonth() + 1);
  return `${month}月登录赠送`;
}

async function requestUsage(headers, host) {
  const url = `${host}${USAGE_PATH}`;
  const body = JSON.stringify({ require_usage: true, req_source: 2 });
  if (DEBUG) console.log(`诊断：查询积分 usage ${url}`);
  const data = await requestJson(url, headers, body);
  const result = unwrap(data);
  const code = data?.code ?? result?.code;
  if (code !== undefined && Number(code) !== 0) {
    throw new Error(`${getErrorMessage(data)}（code: ${code}）`);
  }

  const packs = Array.isArray(result.user_entitlement_pack_list)
    ? result.user_entitlement_pack_list
    : [];
  const rows = packs.flatMap((pack) => {
    const base = pack?.entitlement_base_info || {};
    const quota = base?.quota || {};
    const usage = pack?.usage || {};
    const limit = toFiniteNumber(quota.credits_limit);
    const used = toFiniteNumber(usage.credits_amount) ?? 0;
    const fallbackRow = {
      name: pack?.display_desc || '未命名权益包',
      limit,
      used,
      expire: base.end_time,
    };
    const detailRows = extractDailyDetailRows(pack, base);
    return detailRows.length ? detailRows : [fallbackRow];
  });

  const numericRows = rows.filter(row => row.limit !== null);
  const totalLimit = numericRows.reduce((sum, row) => sum + row.limit, 0);
  const totalUsed = numericRows.reduce((sum, row) => sum + row.used, 0);
  return { rows, totalLimit, totalUsed, totalRemain: totalLimit - totalUsed };
}

function formatUsageSummary(name, usage) {
  if (!usage) return '';
  const lines = [];
  const rows = compactUsageRows(usage.rows);
  if (rows.length === 0) {
    lines.push(`${name}：未获取到有明确剩余额度的积分权益包`);
  } else {
    lines.push(`${name}：剩余可用总积分 ${formatCredits(usage.totalRemain)}`);
    for (const row of rows) {
      const remain = row.limit === null ? '未知' : formatCredits(row.limit - row.used);
      const isMonthlyLogin = /每月登录赠送/.test(String(row.name || ''));
      const displayName = isMonthlyLogin ? monthlyLoginLabel() : row.name;
      const expiry = row.name === '每日签到' ? dailyExpiryText(row) : '';
      lines.push(`  ${displayName}：剩余 ${remain}${expiry}`);
    }
  }
  return `\n${lines.join('\n')}`;
}

async function getUsageSummary(headers, host, name) {
  if (!BALANCE_ENABLED) return '';
  try {
    const usage = await requestUsage(headers, host);
    return formatUsageSummary(name, usage);
  } catch (error) {
    const message = summarizeNotifyError(error);
    if (DEBUG) console.log(`诊断：积分 usage 查询失败：${message}`);
    return `\n${name}：积分余额查询失败（${message}）`;
  }
}

async function checkOne(account) {
  const name = account.name || '未命名账号';
  const headers = normalizeHeaders(account.headers);

  if (!headers.Cookie && !headers.cookie && !headers.Authorization && !headers.authorization) {
    throw new Error('未配置 Cookie 或 Authorization，请先配置 TRAE_HEADERS / TRAE_AUTHORIZATION / TRAE_COOKIE / TRAE_TOKEN');
  }

  const statusResult = await requestStatus(headers);
  const status = statusResult.data;
  const enabled = status.enable ?? status.enabled ?? status.checkin_enabled;
  const checkedIn = status.checked_in ?? status.checkedIn;

  if (enabled === false) {
    const message = getErrorMessage(status);
    if (Number(status.code) === 1001 || /authenticate|认证|登录|token|token/i.test(message)) {
      throw new Error(`认证失败：${message}；请将实际签到请求中的 authorization 原值配置到 TRAE_AUTHORIZATION，不能只填普通 Cookie`);
    }
    throw new Error(`当前账号不满足签到条件：${message}`);
  }
  if (checkedIn === true) {
    const balance = await getUsageSummary(headers, statusResult.host, name);
    return `${name}：今天已经签到${status.credits ? `，积分 ${status.credits}` : ''}${balance}`;
  }

  const claimUrl = `${statusResult.host}${CLAIM_PATH}`;
  // 以当前客户端抓到的成功请求为准；未配置时发送空 JSON 对象。
  const claimBody = parseOptionalBody('TRAE_CLAIM_BODY') ?? '{}';
  if (DEBUG) {
    console.log(`诊断：调用领取接口 ${claimUrl}`);
    console.log(`诊断：领取请求体 ${claimBody === '{}' ? '{}（默认）' : '已设置'}`);
  }
  const claim = await requestJson(claimUrl, headers, claimBody);
  const result = unwrap(claim);
  const code = claim?.code ?? result?.code;
  if (code !== undefined && Number(code) !== 0) {
    const message = getErrorMessage(claim);
    if (Number(code) === 9004) {
      throw new Error(`${message}（code: 9004）；当前 Token 可用，但 claim 参数与客户端状态不匹配。请在 TRAE_ACCOUNTS 中补充真实成功请求的 deviceId（对应 x-device-id）和必要的客户端 headers；请求体默认使用 {}，只有真实成功请求不同才设置 TRAE_CLAIM_BODY；不要连续重复重试`);
    }
    throw new Error(`${message}（code: ${code}）`);
  }

  const credits = result.credits ?? result.credit ?? result.granted_credits;
  const balance = await getUsageSummary(headers, statusResult.host, name);
  return `${name}：签到成功${credits ? `，获得 ${credits} 积分` : ''}${balance}`;
}

function resolveNotifier(moduleValue) {
  if (typeof moduleValue === 'function') return moduleValue;
  if (moduleValue && typeof moduleValue.sendNotify === 'function') return moduleValue.sendNotify.bind(moduleValue);
  if (moduleValue?.default) return resolveNotifier(moduleValue.default);
  return null;
}

function summarizeNotifyError(error, depth = 0) {
  if (!error || depth > 2) return '未知通知错误';
  const message = error.message || String(error);
  if (Array.isArray(error.errors) && error.errors.length) {
    const children = error.errors.map(child => summarizeNotifyError(child, depth + 1)).join(' | ');
    return `${message} [${children}]`;
  }
  return message.replace(/(authorization|cookie|token)\s*[:=]\s*[^\s,;]+/gi, '$1=<redacted>');
}

async function withScriptDirectory(callback) {
  const previousCwd = process.cwd();
  try {
    // 兼容 sendNotify.js 内部使用 ./notify.py、./config 等相对路径的通知脚本。
    process.chdir(__dirname);
    return await callback();
  } finally {
    process.chdir(previousCwd);
  }
}

function pythonNotifyRoots() {
  const qlDir = process.env.QL_DIR || '/ql';
  return [...new Set([
    __dirname,
    path.dirname(__dirname),
    path.join(qlDir, 'data/scripts'),
    path.join(qlDir, 'scripts'),
    ...(process.env.TRAE_PYTHON_PATHS || '').split(/[\n,]+/).filter(Boolean),
  ])];
}

function sendWithQinglongPython(title, content) {
  const pythonBin = process.env.TRAE_PYTHON_BIN || process.env.PYTHON_BIN || 'python3';
  const roots = pythonNotifyRoots();
  const pythonPath = [
    ...roots,
    process.env.PYTHONPATH || '',
  ].filter(Boolean).join(path.delimiter);
  const code = [
    'import sys',
    'from utils.notify import send',
    'send(sys.argv[1], sys.argv[2])',
  ].join('\n');
  const result = spawnSync(pythonBin, ['-c', code, title, content], {
    cwd: __dirname,
    env: { ...process.env, PYTHONPATH: pythonPath },
    encoding: 'utf8',
    timeout: Number(process.env.TRAE_NOTIFY_TIMEOUT || 15000),
    maxBuffer: 1024 * 1024,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const detail = String(result.stderr || result.stdout || '').trim().slice(-1200);
    throw new Error(`Python 通知退出码 ${result.status}${detail ? `：${detail}` : ''}`);
  }
}

async function sendNotification(title, content) {
  if (process.env.TRAE_NOTIFY === '0') return;

  const errors = [];
  try {
    sendWithQinglongPython(title, content);
    if (DEBUG) console.log('通知：已调用青龙 Python utils.notify.send');
    return;
  } catch (error) {
    errors.push(`青龙 Python 通知失败：${summarizeNotifyError(error)}`);
  }

  try {
    if (typeof global.$notify === 'function') {
      await Promise.resolve(global.$notify(title, '', content));
      return;
    }
  } catch (error) {
    console.log(`系统通知发送失败：${error.message || error}`);
  }

  const candidates = [
    process.env.QL_SEND_NOTIFY,
    path.join(__dirname, 'sendNotify.js'),
    path.join(process.cwd(), 'sendNotify.js'),
    '/ql/scripts/sendNotify.js',
    '/ql/data/scripts/sendNotify.js',
    '/ql/deps/sendNotify.js',
  ].filter(Boolean);

  const tried = new Set();
  let foundNotifier = false;
  for (const candidate of candidates) {
    const modulePath = path.isAbsolute(candidate) ? candidate : path.resolve(__dirname, candidate);
    if (tried.has(modulePath)) continue;
    tried.add(modulePath);

    try {
      const loaded = await withScriptDirectory(() => require(modulePath));
      const notifier = resolveNotifier(loaded);
      if (notifier) {
        foundNotifier = true;
        try {
          await withScriptDirectory(() => Promise.resolve(notifier(title, content)));
          return;
        } catch (error) {
          errors.push(`${modulePath}: 通知函数调用失败：${summarizeNotifyError(error)}`);
          continue;
        }
      }
      errors.push(`${modulePath}: 未导出 sendNotify 函数`);
    } catch (error) {
      errors.push(`${modulePath}: 模块加载失败：${summarizeNotifyError(error)}`);
    }
  }

  if (DEBUG) {
    console.log(`通知模块诊断（工作目录 ${process.cwd()}，脚本目录 ${__dirname}）：${errors.join(' | ')}`);
  }
  if (foundNotifier) {
    console.log('通知模块已找到，但通知函数调用失败，不影响签到结果。');
  } else {
    console.log('青龙通知模块不可用（未找到可调用的 sendNotify 导出），不影响签到结果。');
  }
}

async function main() {
  await preparePersistentAuth();
  const accounts = buildAccounts();
  const results = [];
  let failed = false;
  const today = beijingDate();
  const state = readRunState();
  if (state.date !== today) {
    for (const key of Object.keys(state)) delete state[key];
    state.date = today;
  }

  console.log(`Trae Work 签到开始，共 ${accounts.length} 个账号`);
  if (DEBUG) console.log(`诊断：API 主机候选 ${API_HOSTS.join(' , ')}`);
  const jitterMax = Math.max(0, Number(process.env.TRAE_JITTER || 20));
  if (jitterMax > 0) {
    const jitter = Math.floor(Math.random() * (jitterMax + 1));
    if (jitter > 0) {
      console.log(`启动随机错峰等待 ${jitter} 秒`);
      await sleep(jitter * 1000);
    }
  }

  let attemptedClaim = false;
  for (let index = 0; index < accounts.length; index += 1) {
    const account = accounts[index];
    const name = account.name || '未命名账号';
    const key = accountStateKey(account, index);
    const saved = state[key] || {};
    if (SKIP_DONE && saved.date === today && saved.done === true) {
      const message = `${name}：今天已经处理，跳过重复请求`;
      console.log(message);
      results.push(`成功：${message}`);
      continue;
    }
    if (isCoolingDown(state, key)) {
      const minutes = Math.ceil((state[key].cooldownUntil - Date.now()) / 60000);
      const message = `${name}：触发限流，冷却中，约 ${minutes} 分钟后重试`;
      console.log(message);
      results.push(`待重试：${message}`);
      continue;
    }
    if (attemptedClaim && GAP_MAX > 0) {
      const gap = GAP_MIN + Math.floor(Math.random() * (GAP_MAX - GAP_MIN + 1));
      if (gap > 0) {
        console.log(`账号间错峰等待 ${gap} 秒`);
        await sleep(gap * 1000);
      }
    }
    try {
      const result = await checkOne(account);
      console.log(result);
      results.push(`成功：${result}`);
      state[key] = { date: today, done: true, updatedAt: Date.now() };
      writeRunState(state);
    } catch (error) {
      const message = `${name}：${error.message || error}`;
      if (isRateLimited(error)) {
        markCooldown(state, key, message);
        writeRunState(state);
        console.log(`待重试：${message}；已冷却 ${COOLDOWN_MIN} 分钟`);
        results.push(`待重试：${message}`);
      } else {
        failed = true;
        console.log(`失败：${message}`);
        results.push(`失败：${message}`);
      }
    }
    attemptedClaim = true;
  }

  writeRunState(state);
  const content = results.join('\n');
  await sendNotification('Trae Work 每日签到', content);
  if (failed) process.exitCode = 1;
}

main().catch(async (error) => {
  console.log(`脚本执行失败：${error.message || error}`);
  await sendNotification('Trae Work 每日签到', `脚本执行失败：${error.message || error}`);
  process.exitCode = 1;
});
