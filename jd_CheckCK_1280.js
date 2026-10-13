/**
 * 京东CK检测脚本（独立版 v2，支持青龙API检测已禁用CK）
 *
 * 功能说明：
 *   - 通过青龙面板API获取所有CK（包括已禁用的），逐一检测有效性
 *   - CK有效：不通知，静默结束
 *   - CK失效：每次检测都发送通知
 *   - 可选：自动禁用失效CK / 自动启用恢复CK
 *
 * 青龙API配置（用于检测已禁用的CK）：
 *   方式一：在青龙中直接运行（自动使用内置QLAPI，无需额外配置）
 *   方式二：独立运行时配置HTTP API环境变量：
 *     QL_URL              - 青龙面板地址（如 http://127.0.0.1:5700）
 *     QL_CLIENT_ID        - 青龙应用ID（系统设置→应用设置中创建）
 *     QL_CLIENT_SECRET    - 青龙应用密钥
 *     QL_TOKEN            - （可选）直接提供Token，跳过获取步骤
 *   如不配置青龙API，脚本将使用 JD_COOKIE 环境变量（仅能检测启用的CK）
 *
 * 环境变量：
 *   JD_COOKIE            (青龙API不可用时使用) 京东Cookie，格式: pt_key=xxx;pt_pin=xxx;
 *   jd_checkck_interval (可选) 检测间隔毫秒数，默认1000
 *   jd_checkck_threads  (可选) 并发线程数，默认1，最大50
 *   jd_checkck_timeout  (可选) 单次请求超时毫秒数，默认10000
 *   jd_checkck_showok   (可选) 控制台是否显示正常账号，默认 true
 *   jd_checkck_autodisable (可选) 自动禁用失效CK，默认 false
 *   jd_checkck_autoenable  (可选) 自动启用恢复CK，默认 false
 *   jd_checkck_search  (可选) 青龙环境变量搜索关键词，默认 JD_COOKIE
 *
 * 通知渠道（自动优先使用青龙面板通知）：
 *   方式一：在青龙中运行，自动使用 QLAPI.systemNotify 或 ./sendNotify.js
 *   方式二：配置以下环境变量，使用内置通知：
 *     BARK_PUSH                        - Bark推送地址或key
 *     TG_BOT_TOKEN + TG_USER_ID       - Telegram机器人
 *     PUSH_KEY                         - Server酱SCKEY
 *     PUSH_PLUS_TOKEN [+ PUSH_PLUS_USER] - PushPlus
 *     DD_BOT_TOKEN [+ DD_BOT_SECRET]  - 钉钉机器人
 *     QYWX_AM                          - 企业微信应用(corpid,corpsecret,agentid,touser)
 *
 * cron: 30 2-22/2 * * *
 */

'use strict'

const https = require('https')
const http = require('http')
const zlib = require('zlib')
const crypto = require('crypto')

// ======================== 配置读取 ========================

const JD_COOKIE = process.env.JD_COOKIE || ''
const CHECK_INTERVAL = parseInt(process.env.jd_checkck_interval || '1000')
const THREADS = Math.min(Math.max(parseInt(process.env.jd_checkck_threads || '1'), 1), 50)
const REQUEST_TIMEOUT = parseInt(process.env.jd_checkck_timeout || '10000')
const SHOW_OK = (process.env.jd_checkck_showok || 'true') !== 'false'
const AUTO_DISABLE = process.env.jd_checkck_autodisable === 'true'
const AUTO_ENABLE = process.env.jd_checkck_autoenable === 'true'
const QL_SEARCH = process.env.jd_checkck_search || 'JD_COOKIE'

// 京东API常量
const JD_CHECK_URL = 'https://me-api.jd.com/user_new/info/GetJDUserInfoUnion?orgFlag=JD0515145'
const JD_UA = 'jdapp;iPhone;11.6.2;15.0;network/wifi;Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'

// ======================== HTTP 工具 ========================

/** 通用 HTTP/HTTPS 请求，支持 gzip/deflate/br 自动解压 */
function fetch(url, options = {}) {
  const { method = 'GET', headers = {}, body, timeout = REQUEST_TIMEOUT } = options
  return new Promise((resolve, reject) => {
    const u = new URL(url)
    const lib = u.protocol === 'https:' ? https : http
    const opts = {
      hostname: u.hostname,
      port: u.port || (u.protocol === 'https:' ? 443 : 80),
      path: u.pathname + u.search,
      method,
      headers: { ...headers },
      rejectUnauthorized: false,
    }
    const req = lib.request(opts, (res) => {
      const chunks = []
      let stream = res
      const enc = res.headers['content-encoding']
      if (enc === 'gzip') {
        stream = res.pipe(zlib.createGunzip())
      } else if (enc === 'deflate') {
        stream = res.pipe(zlib.createInflate())
      } else if (enc === 'br') {
        stream = res.pipe(zlib.createBrotliDecompress())
      }
      stream.on('data', (c) => chunks.push(c))
      stream.on('end', () => {
        resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString('utf8') })
      })
      stream.on('error', reject)
    })
    req.on('error', reject)
    req.setTimeout(timeout, () => req.destroy(new Error('请求超时')))
    if (body) req.write(body)
    req.end()
  })
}

// ======================== 日期工具 ========================

/** 格式化当前时间为 24小时制: 2026-09-03 17:57:36 */
function formatDateTime() {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).formatToParts(new Date())
  const get = (t) => parts.find((p) => p.type === t)?.value || '00'
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')}`
}

// ======================== Cookie 工具 ========================

/** 从 Cookie 字符串中提取 pt_pin（用户标识） */
function getPin(cookie) {
  const m = cookie.match(/pt_pin=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : '未知'
}

/** 从 JD_COOKIE 环境变量解析出所有 Cookie（仅含启用的，作为备用方案） */
function getCookiesFromEnv() {
  if (!JD_COOKIE) return []
  return JD_COOKIE.split(/[\n&]/)
    .map((c) => c.trim())
    .filter((c) => c && c.includes('pt_pin='))
}

// ======================== 青龙面板 API ========================

const qlApi = {
  mode: null,       // 'builtin' | 'http' | null
  qlUrl: '',
  token: '',
  clientId: '',
  clientSecret: '',

  /** 检测青龙API是否可用 */
  isAvailable() {
    // 方式一：内置 QLAPI 全局变量（在青龙中运行时自动注入）
    if (typeof QLAPI !== 'undefined' && QLAPI) {
      this.mode = 'builtin'
      return true
    }
    // 方式二：HTTP API 配置
    const qlUrl = process.env.QL_URL
    const clientId = process.env.QL_CLIENT_ID
    const clientSecret = process.env.QL_CLIENT_SECRET
    const qlToken = process.env.QL_TOKEN
    if (qlUrl && (qlToken || (clientId && clientSecret))) {
      this.mode = 'http'
      this.qlUrl = qlUrl.replace(/\/$/, '')
      this.token = qlToken || ''
      this.clientId = clientId || ''
      this.clientSecret = clientSecret || ''
      return true
    }
    return false
  },

  /** 获取HTTP API的Token */
  async getToken() {
    if (this.token) return this.token
    const res = await fetch(
      `${this.qlUrl}/open/auth/token?client_id=${this.clientId}&client_secret=${this.clientSecret}`
    )
    const json = JSON.parse(res.body)
    if (json.code === 200 && json.data && json.data.token) {
      this.token = json.data.token
      return this.token
    }
    throw new Error(`获取Token失败: ${json.message || res.body}`)
  },

  /** 获取环境变量列表（包含已禁用的） */
  async getEnvs(searchValue) {
    if (this.mode === 'builtin') {
      return await QLAPI.getEnvs({ searchValue })
    }
    const token = await this.getToken()
    const res = await fetch(
      `${this.qlUrl}/open/envs?searchValue=${encodeURIComponent(searchValue)}`,
      { headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' } }
    )
    return JSON.parse(res.body)
  },

  /** 禁用环境变量 */
  async disableEnvs(ids) {
    if (this.mode === 'builtin') {
      return await QLAPI.disableEnvs({ ids })
    }
    const token = await this.getToken()
    const res = await fetch(`${this.qlUrl}/open/envs/disable`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(ids),
    })
    return JSON.parse(res.body)
  },

  /** 启用环境变量 */
  async enableEnvs(ids) {
    if (this.mode === 'builtin') {
      return await QLAPI.enableEnvs({ ids })
    }
    const token = await this.getToken()
    const res = await fetch(`${this.qlUrl}/open/envs/enable`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(ids),
    })
    return JSON.parse(res.body)
  },

  /** 通过青龙系统发送通知 */
  async systemNotify(title, content) {
    if (this.mode === 'builtin' && typeof QLAPI.systemNotify === 'function') {
      try {
        await QLAPI.systemNotify({ title, content })
        return true
      } catch (e) {
        console.log(`QLAPI.systemNotify 失败: ${e.message}`)
      }
    }
    return false
  },
}

/** 从API响应中解析环境变量数组 */
function parseEnvsData(res) {
  let data = res
  if (typeof data === 'string') {
    try { data = JSON.parse(data) } catch { return [] }
  }
  if (Array.isArray(data)) return data
  if (data && Array.isArray(data.data)) return data.data
  if (data && data.data && Array.isArray(data.data.data)) return data.data.data
  return []
}

// ======================== CK 检测核心 ========================

/**
 * 检测单个京东Cookie是否有效
 * @param {string} cookie  Cookie字符串
 * @param {number} index   账号序号
 * @param {object|null} qlInfo  青龙环境变量信息 {id, status, remarks}
 */
async function checkCookie(cookie, index, qlInfo) {
  const pin = getPin(cookie)
  try {
    const res = await fetch(JD_CHECK_URL, {
      headers: {
        Cookie: cookie,
        'User-Agent': JD_UA,
        Accept: '*/*',
        'Accept-Language': 'zh-CN,zh-Hans;q=0.9',
        Connection: 'keep-alive',
        Referer: 'https://home.m.jd.com/',
        Host: 'me-api.jd.com',
      },
    })

    if (res.status !== 200) {
      return { index, pin, qlInfo, status: 'error', msg: `HTTP状态码异常: ${res.status}` }
    }

    let json
    try {
      json = JSON.parse(res.body)
    } catch {
      // 响应非JSON，可能被风控拦截
      const preview = res.body.substring(0, 200)
      return { index, pin, qlInfo, status: 'error', msg: `响应解析失败(可能被风控): ${preview}` }
    }

    // 兼容字段名大小写: retCode / retcode / code
    // 兼容类型: 数字0 和 字符串"0"
    const retCode = String(json.retCode ?? json.retcode ?? json.code ?? '')
    const data = json.data || {}
    // userInfo 可能在 data.userInfo 或 data.data.userInfo
    const userInfo = data.userInfo || (data.data && data.data.userInfo)

    // 调试输出：打印API返回的关键字段
    const debugInfo = `retCode=${retCode}, hasUserInfo=${!!userInfo}, bodyPreview=${res.body.substring(0, 150)}`

    // retCode=0 且有 userInfo → CK 有效
    if (retCode === '0' && userInfo) {
      const baseInfo = userInfo.baseInfo || {}
      const nickname = baseInfo.nickname || baseInfo.curNickname || pin
      console.log(`   账号${index}: ${pin} ✅ 有效 (retCode=${retCode})`)
      return { index, pin, qlInfo, status: 'valid', nickname }
    }

    // 其他情况 → CK 失效（打印调试信息帮助排查）
    const msg = json.retMessage || json.retmessage || json.message || ''
    console.log(`   账号${index}: ${pin} ❌ 失效 (${debugInfo})`)
    return {
      index, pin, qlInfo, status: 'expired',
      msg: `CK已失效${retCode ? `(retCode=${retCode})` : ''}${msg ? ` ${msg}` : ''}`,
    }
  } catch (e) {
    console.log(`   账号${index}: ${pin} ⚠️ 出错: ${e.message}`)
    return { index, pin, qlInfo, status: 'error', msg: `检测出错: ${e.message}` }
  }
}

// ======================== 并发控制 ========================

async function runConcurrent(tasks, concurrency) {
  const results = new Array(tasks.length)
  let cursor = 0

  async function worker() {
    while (cursor < tasks.length) {
      const idx = cursor++
      results[idx] = await tasks[idx]()
      if (cursor < tasks.length && CHECK_INTERVAL > 0) {
        await new Promise((r) => setTimeout(r, CHECK_INTERVAL))
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, tasks.length) }, () => worker()))
  return results
}

// ======================== 通知发送 ========================

async function sendNotify(title, content) {
  // 优先级1：青龙内置 systemNotify
  if (qlApi.mode === 'builtin') {
    const ok = await qlApi.systemNotify(title, content)
    if (ok) { console.log('📧 已通过青龙 systemNotify 发送通知'); return }
  }

  // 优先级2：青龙 sendNotify.js
  try {
    const m = require('./sendNotify')
    const fn = typeof m === 'function' ? m : m.sendNotify || m.default
    if (typeof fn === 'function') {
      console.log('📧 使用 sendNotify.js 发送通知...')
      await fn(title, content)
      return
    }
  } catch { /* 不存在，使用内置渠道 */ }

  // 优先级3：内置通知渠道
  const results = []

  // --- Bark ---
  if (process.env.BARK_PUSH) {
    try {
      let url = process.env.BARK_PUSH.trim().replace(/\n/g, '').replace(/\/$/, '')
      if (!url.startsWith('http')) url = `https://api.day.app/${url}`
      await fetch(`${url}/${encodeURIComponent(title)}/${encodeURIComponent(content)}`)
      results.push('Bark ✅')
    } catch (e) { results.push(`Bark ❌ ${e.message}`) }
  }

  // --- Telegram ---
  if (process.env.TG_BOT_TOKEN && process.env.TG_USER_ID) {
    try {
      await fetch(`https://api.telegram.org/bot${process.env.TG_BOT_TOKEN}/sendMessage`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: process.env.TG_USER_ID, text: `${title}\n\n${content}`, parse_mode: 'HTML' }),
      })
      results.push('Telegram ✅')
    } catch (e) { results.push(`Telegram ❌ ${e.message}`) }
  }

  // --- Server酱 ---
  if (process.env.PUSH_KEY) {
    try {
      await fetch(`https://sctapi.ftqq.com/${process.env.PUSH_KEY}.send`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, desp: content }),
      })
      results.push('Server酱 ✅')
    } catch (e) { results.push(`Server酱 ❌ ${e.message}`) }
  }

  // --- PushPlus ---
  if (process.env.PUSH_PLUS_TOKEN) {
    try {
      await fetch('http://www.pushplus.plus/send', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: process.env.PUSH_PLUS_TOKEN, title, content, template: 'html', topic: process.env.PUSH_PLUS_USER || '' }),
      })
      results.push('PushPlus ✅')
    } catch (e) { results.push(`PushPlus ❌ ${e.message}`) }
  }

  // --- 钉钉 ---
  if (process.env.DD_BOT_TOKEN) {
    try {
      let url = `https://oapi.dingtalk.com/robot/send?access_token=${process.env.DD_BOT_TOKEN}`
      if (process.env.DD_BOT_SECRET) {
        const ts = Date.now()
        const sign = crypto.createHmac('sha256', process.env.DD_BOT_SECRET).update(`${ts}\n${process.env.DD_BOT_SECRET}`).digest('base64')
        url += `&timestamp=${ts}&sign=${encodeURIComponent(sign)}`
      }
      await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ msgtype: 'text', text: { content: `${title}\n\n${content}` } }),
      })
      results.push('钉钉 ✅')
    } catch (e) { results.push(`钉钉 ❌ ${e.message}`) }
  }

  // --- 企业微信 ---
  if (process.env.QYWX_AM) {
    try {
      const [corpid, corpsecret, agentid, touser] = process.env.QYWX_AM.split(',')
      const tokenRes = await fetch(`https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=${corpid}&corpsecret=${corpsecret}`)
      const tokenJson = JSON.parse(tokenRes.body)
      if (tokenJson.access_token) {
        await fetch(`https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=${tokenJson.access_token}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ touser: touser || '@all', agentid: parseInt(agentid), msgtype: 'text', text: { content: `${title}\n\n${content}` } }),
        })
        results.push('企业微信 ✅')
      }
    } catch (e) { results.push(`企业微信 ❌ ${e.message}`) }
  }

  if (results.length > 0) {
    console.log(`\n📧 通知结果: ${results.join('  |  ')}`)
  } else {
    console.log('\n⚠️ 未检测到通知渠道配置')
    console.log('   配置方式: BARK_PUSH / TG_BOT_TOKEN+TG_USER_ID / PUSH_KEY / PUSH_PLUS_TOKEN / DD_BOT_TOKEN / QYWX_AM')
    console.log('   或在青龙面板中配置通知后直接运行本脚本\n')
    console.log('─── 通知内容 ───')
    console.log(`标题: ${title}`)
    console.log(content)
    console.log('─── 通知内容结束 ───')
  }
}

// ======================== 主函数 ========================

async function main() {
  console.log('🔔 京东CK检测脚本 v2 启动')
  console.log(`   并发: ${THREADS}  间隔: ${CHECK_INTERVAL}ms  超时: ${REQUEST_TIMEOUT}ms`)

  let accounts = []      // [{ cookie, qlInfo }]
  let useQlApi = false

  // === 步骤1：获取账号列表 ===
  if (qlApi.isAvailable()) {
    useQlApi = true
    const modeLabel = qlApi.mode === 'builtin' ? '内置QLAPI' : `HTTP API(${qlApi.qlUrl})`
    console.log(`\n🔗 已连接青龙面板API (${modeLabel})`)
    console.log(`   可检测所有CK（含已禁用的）`)

    try {
      const res = await qlApi.getEnvs(QL_SEARCH)
      const envs = parseEnvsData(res)

      // 筛选包含 pt_pin 的环境变量
      for (const env of envs) {
        const value = env.value || ''
        if (!value.includes('pt_pin=')) continue
        accounts.push({
          cookie: value,
          qlInfo: {
            id: env.id,
            status: env.status,        // 0=启用 1=禁用
            remarks: env.remarks || '',
            name: env.name || '',
          },
        })
      }

      if (accounts.length === 0) {
        console.log(`\n⚠️ 青龙API返回了 ${envs.length} 个环境变量，但未找到包含JD Cookie的`)
        console.log('   回退到 JD_COOKIE 环境变量...')
        useQlApi = false
      }
    } catch (e) {
      console.log(`\n⚠️ 青龙API调用失败: ${e.message}`)
      console.log('   回退到 JD_COOKIE 环境变量...')
      useQlApi = false
    }
  }

  // 回退到环境变量
  if (!useQlApi) {
    const envCookies = getCookiesFromEnv()
    if (envCookies.length === 0) {
      console.log('\n❌ 未检测到京东Cookie！')
      console.log('   请配置 JD_COOKIE 环境变量，或配置青龙面板API')
      console.log('   (QL_URL + QL_CLIENT_ID + QL_CLIENT_SECRET)')
      process.exit(1)
    }
    accounts = envCookies.map((cookie) => ({ cookie, qlInfo: null }))
    console.log('\n📋 使用 JD_COOKIE 环境变量（仅含启用的CK）')
  }

  // 统计启用/禁用数量
  const enabledCount = accounts.filter((a) => !a.qlInfo || a.qlInfo.status === 0).length
  const disabledCount = accounts.filter((a) => a.qlInfo && a.qlInfo.status === 1).length

  console.log(`\n📋 共检测到 ${accounts.length} 个账号（启用${enabledCount}个` +
    (disabledCount > 0 ? `，禁用${disabledCount}个` : '') + '）')

  // === 步骤2：检测所有CK ===
  const tasks = accounts.map((acct, i) => () => checkCookie(acct.cookie, i + 1, acct.qlInfo))
  const results = await runConcurrent(tasks, THREADS)

  // 分类统计
  const validList = results.filter((r) => r.status === 'valid')
  const expiredList = results.filter((r) => r.status === 'expired')
  const errorList = results.filter((r) => r.status === 'error')

  // === 步骤3：自动禁用/启用（仅在使用QL API时） ===
  const autoActions = []

  if (useQlApi && AUTO_DISABLE) {
    const toDisable = expiredList.filter((r) => r.qlInfo && r.qlInfo.status === 0)
    // 网络错误也尝试禁用（可能是CK失效导致请求异常）
    const toDisableErr = errorList.filter((r) => r.qlInfo && r.qlInfo.status === 0)
    const allToDisable = [...toDisable, ...toDisableErr]
    if (allToDisable.length > 0) {
      const ids = allToDisable.map((r) => r.qlInfo.id)
      try {
        await qlApi.disableEnvs(ids)
        allToDisable.forEach((r) => {
          autoActions.push(`已自动禁用: 账号${r.index}(${r.pin})`)
        })
        console.log(`\n🔧 已自动禁用 ${ids.length} 个失效CK`)
      } catch (e) {
        console.log(`\n⚠️ 自动禁用失败: ${e.message}`)
      }
    }
  }

  if (useQlApi && AUTO_ENABLE) {
    // 恢复的CK（之前禁用，现在有效）
    const toEnable = validList.filter((r) => r.qlInfo && r.qlInfo.status === 1)
    if (toEnable.length > 0) {
      const ids = toEnable.map((r) => r.qlInfo.id)
      try {
        await qlApi.enableEnvs(ids)
        toEnable.forEach((r) => {
          autoActions.push(`已自动启用: 账号${r.index}(${r.pin})`)
        })
        console.log(`\n🔧 已自动启用 ${ids.length} 个恢复CK`)
      } catch (e) {
        console.log(`\n⚠️ 自动启用失败: ${e.message}`)
      }
    }
  }

  // === 步骤4：打印检测结果 ===
  const statusLabel = (qlInfo) => {
    if (!qlInfo) return ''
    return qlInfo.status === 1 ? ' [已禁用]' : ' [启用中]'
  }

  console.log('\n══════════════════════════════════════')
  console.log(`📊 检测结果: 共${results.length}个  ✅正常${validList.length}  ❌失效${expiredList.length}  ⚠️出错${errorList.length}`)
  console.log('══════════════════════════════════════\n')

  if (SHOW_OK && validList.length > 0) {
    console.log('✅ 正常账号:')
    validList.forEach((r) => {
      console.log(`   账号${r.index}: ${r.nickname || r.pin}${statusLabel(r.qlInfo)}`)
    })
    console.log('')
  }

  if (expiredList.length > 0) {
    console.log('❌ 失效账号:')
    expiredList.forEach((r) => {
      console.log(`   账号${r.index}: ${r.pin}${statusLabel(r.qlInfo)} → ${r.msg}`)
    })
    console.log('')
  }

  if (errorList.length > 0) {
    console.log('⚠️ 检测出错:')
    errorList.forEach((r) => {
      console.log(`   账号${r.index}: ${r.pin}${statusLabel(r.qlInfo)} → ${r.msg}`)
    })
    console.log('')
  }

  // === 步骤5：通知逻辑（核心） ===
  // CK有效: 不通知
  // CK失效: 每次检测都通知（包括已禁用的失效CK）
  if (expiredList.length > 0 || errorList.length > 0) {
    const now = formatDateTime()

    let content = `\n检测时间: ${now}\n`
    content += `账号总数: ${results.length}  正常: ${validList.length}  失效: ${expiredList.length}  出错: ${errorList.length}\n`

    if (expiredList.length > 0) {
      content += `\n❌ 失效账号 (${expiredList.length}个):\n`
      expiredList.forEach((r) => {
        content += `   账号${r.index}: ${r.pin}${statusLabel(r.qlInfo)} → ${r.msg}\n`
      })
    }

    if (errorList.length > 0) {
      content += `\n⚠️ 检测出错 (${errorList.length}个):\n`
      errorList.forEach((r) => {
        content += `   账号${r.index}: ${r.pin}${statusLabel(r.qlInfo)} → ${r.msg}\n`
      })
    }

    if (autoActions.length > 0) {
      content += `\n🔧 自动操作:\n`
      autoActions.forEach((a) => { content += `   ${a}\n` })
    }

    content += '\n⏰ 请及时更新失效账号的CK！'

    await sendNotify('京东CK检测 - 发现失效账号', content)
  } else {
    console.log('✅ 所有账号CK状态正常，无需通知')
  }

  console.log('\n🔔 京东CK检测脚本 结束')
}

main().catch((e) => {
  console.error('❌ 脚本运行遇到错误:', e)
  process.exit(1)
})
