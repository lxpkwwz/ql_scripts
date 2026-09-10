// ============================================================
// OpenList 存储状态监控脚本 (青龙面板)
// 定时检测 OpenList 挂载的网盘存储状态，发现失效时通过青龙自带通知推送提醒
// ------------------------------------------------------------
// == 环境变量配置 ==
// OPENLIST_URL          OpenList 服务地址，默认 http://172.17.0.1:5244/
// OPENLIST_USERNAME     OpenList 管理员用户名 (与密码配合使用)
// OPENLIST_PASSWORD     OpenList 管理员密码 (会自动 SHA256 加密后传输)
// OPENLIST_TOKEN        OpenList API Token (可选，设置后跳过登录直接使用)
// OPENLIST_MOUNT_PATHS  需监控的挂载路径，逗号分隔，默认 /gyyp_dx,/gyyp_lt
// OPENLIST_NOTIFY_ALL   正常时是否也通知，true/false，默认 false (仅异常时通知)
// ------------------------------------------------------------
// cron: 0 0-23/6 * * *
// new Env('OpenList存储监控')
// ------------------------------------------------------------
// == 使用方法 ==
// 1. 将本脚本放入青龙面板「脚本管理」
// 2. 在「环境变量」中添加 OPENLIST_USERNAME、OPENLIST_PASSWORD
// 3. 在「定时任务」中添加定时规则，如 0 0-23/6 * * * (每6小时检测一次)
// 4. 在青龙「系统设置 -> 通知设置」中配置通知渠道(钉钉/TG/Bark等)
// ============================================================

const crypto = require('crypto')
const http = require('http')
const https = require('https')
const path = require('path')
const fs = require('fs')

// ============ 加载青龙通知模块 ============
// 青龙面板中 sendNotify.js 可能位于多个路径，逐一尝试
let notify = null
const notifyPaths = [
  './sendNotify',
  path.join(process.cwd(), 'sendNotify'),
  '/ql/data/scripts/sendNotify',
  '/ql/scripts/sendNotify',
  '/ql/data/config/sendNotify'
]
for (const p of notifyPaths) {
  try {
    notify = require(p)
    if (notify && typeof notify.sendNotify === 'function') {
      console.log(`[通知] 已加载通知模块: ${p}`)
      break
    }
    notify = null
  } catch (e) {
    // 继续尝试下一个路径
  }
}
if (!notify) {
  console.log('[通知] 未找到 sendNotify 模块，通知功能不可用。')
  console.log('[通知] 尝试过的路径: ' + notifyPaths.join(', '))
  console.log('[通知] 请确认脚本在青龙面板环境中运行，或手动放置 sendNotify.js 到脚本同目录')
}

// ============ 读取环境变量 ============
const OPENLIST_URL = (process.env.OPENLIST_URL || 'http://172.17.0.1:5244/').replace(/\/+$/, '')
const OPENLIST_USERNAME = process.env.OPENLIST_USERNAME || ''
const OPENLIST_PASSWORD = process.env.OPENLIST_PASSWORD || ''
const OPENLIST_TOKEN = process.env.OPENLIST_TOKEN || ''
const MOUNT_PATHS = (process.env.OPENLIST_MOUNT_PATHS || '/gyyp_dx,/gyyp_lt')
  .split(',')
  .map(p => p.trim())
  .filter(Boolean)
const NOTIFY_ALL = process.env.OPENLIST_NOTIFY_ALL === 'true'

// ============ HTTP 请求工具函数 ============
function httpGet(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https') ? https : http
    const options = { headers }
    if (url.startsWith('https')) {
      options.rejectUnauthorized = false
    }
    const req = lib.get(url, options, (res) => {
      let data = ''
      res.on('data', chunk => { data += chunk })
      res.on('end', () => {
        try {
          resolve({ statusCode: res.statusCode, body: JSON.parse(data) })
        } catch (e) {
          resolve({ statusCode: res.statusCode, body: data })
        }
      })
    })
    req.on('error', reject)
    req.setTimeout(20000, () => {
      req.destroy(new Error('请求超时'))
    })
  })
}

function httpPost(url, postData, headers = {}) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https') ? https : http
    const urlObj = new URL(url)
    const options = {
      hostname: urlObj.hostname,
      port: urlObj.port,
      path: urlObj.pathname + urlObj.search,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(postData),
        ...headers
      }
    }
    if (url.startsWith('https')) {
      options.rejectUnauthorized = false
    }
    const req = lib.request(options, (res) => {
      let data = ''
      res.on('data', chunk => { data += chunk })
      res.on('end', () => {
        try {
          resolve({ statusCode: res.statusCode, body: JSON.parse(data) })
        } catch (e) {
          resolve({ statusCode: res.statusCode, body: data })
        }
      })
    })
    req.on('error', reject)
    req.setTimeout(20000, () => {
      req.destroy(new Error('请求超时'))
    })
    req.write(postData)
    req.end()
  })
}

// ============ 登录 OpenList 获取 Token ============
async function loginOpenList() {
  const hashedPassword = crypto.createHash('sha256').update(OPENLIST_PASSWORD).digest('hex')
  const loginBody = JSON.stringify({
    username: OPENLIST_USERNAME,
    password: hashedPassword
  })

  const res = await httpPost(`${OPENLIST_URL}/api/auth/login`, loginBody, {
    'Content-Type': 'application/json'
  })

  if (res.statusCode === 200 && res.body && res.body.code === 200) {
    return res.body.data.token
  }

  throw new Error(`登录失败: ${res.body && res.body.message ? res.body.message : JSON.stringify(res.body)}`)
}

// ============ 获取存储列表 (获取驱动、备注等元信息) ============
async function getStorageList(token) {
  const res = await httpGet(
    `${OPENLIST_URL}/api/admin/storage/list?page=1&per_page=1000`,
    { Authorization: token }
  )

  if (res.statusCode === 200 && res.body && res.body.code === 200) {
    return res.body.data.content
  }

  throw new Error(`获取存储列表失败: ${res.body && res.body.message ? res.body.message : JSON.stringify(res.body)}`)
}

// ============ 实际探测存储是否可用 ============
// 通过 POST /api/fs/list 接口，以 refresh=true 强制刷新绕过缓存，
// 真正访问挂载路径。如果 Token 过期或存储异常，此接口会返回错误。
async function probeStorage(token, mountPath) {
  const body = JSON.stringify({
    path: mountPath,
    page: 1,
    per_page: 1,
    refresh: true
  })

  try {
    const res = await httpPost(`${OPENLIST_URL}/api/fs/list`, body, {
      'Authorization': token,
      'Content-Type': 'application/json'
    })

    if (res.statusCode === 200 && res.body) {
      if (res.body.code === 200) {
        // 接口正常返回，存储可用
        return {
          ok: true,
          message: '存储访问正常',
          detail: `文件列表获取成功`
        }
      } else {
        // code 非 200，存储异常 (Token 过期、初始化失败等)
        return {
          ok: false,
          message: res.body.message || '存储访问失败',
          detail: `错误码: ${res.body.code}, 消息: ${res.body.message || '未知'}`
        }
      }
    } else {
      return {
        ok: false,
        message: `HTTP ${res.statusCode}`,
        detail: `服务返回异常状态码`
      }
    }
  } catch (e) {
    return {
      ok: false,
      message: e.message,
      detail: `请求异常: ${e.message}`
    }
  }
}

// ============ 主逻辑 ============
async function main() {
  const now = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })

  console.log('==================================================')
  console.log('     OpenList 存储状态监控')
  console.log('==================================================')
  console.log(`检测时间: ${now}`)
  console.log(`监控路径: ${MOUNT_PATHS.join(', ')}`)
  console.log(`OpenList: ${OPENLIST_URL}`)
  console.log(`检测方式: 实际访问挂载路径 (强制刷新绕过缓存)`)
  console.log('')

  // ---------- 获取 Token ----------
  let token = OPENLIST_TOKEN
  if (!token) {
    if (!OPENLIST_USERNAME || !OPENLIST_PASSWORD) {
      throw new Error('请设置 OPENLIST_USERNAME + OPENLIST_PASSWORD，或直接设置 OPENLIST_TOKEN')
    }
    console.log('[1/3] 正在登录 OpenList...')
    token = await loginOpenList()
    console.log('      登录成功，已获取 Token')
  } else {
    console.log('[1/3] 使用环境变量中的 Token，跳过登录')
  }

  // ---------- 获取存储列表 (用于获取驱动信息) ----------
  console.log('[2/3] 正在获取存储列表...')
  const allStorages = await getStorageList(token)
  console.log(`      共 ${allStorages.length} 个存储`)

  // 构建挂载路径到存储信息的映射
  const storageMap = {}
  for (const s of allStorages) {
    storageMap[s.mount_path] = s
  }

  // 检查目标路径是否都存在
  const missingPaths = MOUNT_PATHS.filter(p => !storageMap[p])
  if (missingPaths.length > 0) {
    console.log(`      [警告] 以下路径未找到: ${missingPaths.join(', ')}`)
  }
  console.log('')

  // ---------- 逐个实际探测存储状态 ----------
  console.log('[3/3] 正在实际探测存储状态 (强制刷新)...\n')

  const normalList = []
  const abnormalList = []

  for (const mountPath of MOUNT_PATHS) {
    const storage = storageMap[mountPath]
    const driver = storage ? storage.driver : '未知'
    const remark = storage ? (storage.remark || '') : ''
    const storageStatus = storage ? storage.status : '未找到'

    console.log(`--- 探测: ${mountPath} ---`)
    console.log(`  驱动: ${driver}`)
    console.log(`  管理面板状态: ${storageStatus}`)
    console.log(`  禁用标记: ${storage ? (storage.disabled ? '是' : '否') : '未知'}`)

    if (!storage) {
      // 路径不存在
      console.log(`  探测结果: 未找到该挂载路径\n`)
      abnormalList.push({
        mountPath,
        driver,
        storageStatus,
        probeResult: '未找到该挂载路径',
        probeDetail: '该挂载路径在 OpenList 存储列表中不存在',
        remark
      })
      continue
    }

    // 实际探测存储是否可用
    console.log(`  正在请求 /api/fs/list (refresh=true) ...`)
    const probe = await probeStorage(token, mountPath)

    const icon = probe.ok ? '正常' : '异常'
    console.log(`  探测结果: ${icon}`)
    console.log(`  返回消息: ${probe.message}`)
    if (!probe.ok) {
      console.log(`  详细信息: ${probe.detail}`)
    }
    console.log('')

    const info = {
      mountPath,
      driver,
      storageStatus,
      probeResult: probe.message,
      probeDetail: probe.detail,
      remark
    }

    if (probe.ok) {
      normalList.push(info)
    } else {
      abnormalList.push(info)
    }
  }

  // ---------- 构建通知内容 ----------
  const total = MOUNT_PATHS.length
  const normalCount = normalList.length
  const abnormalCount = abnormalList.length

  console.log('==================================================')
  console.log(`检测完成: 正常 ${normalCount}/${total}, 异常 ${abnormalCount}/${total}`)
  console.log('==================================================')

  if (abnormalCount > 0) {
    let message = 'OpenList 存储异常通知\n'
    message += '==================================\n'
    message += `检测时间: ${now}\n`
    message += `OpenList: ${OPENLIST_URL}\n`
    message += `检测路径: ${MOUNT_PATHS.join(', ')}\n`
    message += `异常数量: ${abnormalCount}/${total}\n`
    message += '==================================\n\n'

    abnormalList.forEach((item, idx) => {
      message += `[${idx + 1}] 挂载路径: ${item.mountPath}\n`
      message += `    驱动类型: ${item.driver}\n`
      message += `    管理面板状态: ${item.storageStatus}\n`
      message += `    实际探测结果: ${item.probeResult}\n`
      if (item.probeDetail) {
        message += `    详细信息: ${item.probeDetail}\n`
      }
      if (item.remark) {
        message += `    备注信息: ${item.remark}\n`
      }
      message += '\n'
    })

    message += '==================================\n'
    message += '请尽快登录 OpenList 管理后台处理:\n'
    message += '1. 检查对应存储的 Token/Cookie 是否过期\n'
    message += '2. 重新获取 Token 并更新存储配置\n'
    message += '3. 更新后可再次运行本脚本验证是否恢复\n'

    console.log('\n--- 通知内容 ---')
    console.log(message)

    if (notify) {
      await notify.sendNotify('OpenList 存储异常通知', message)
      console.log('[通知已发送]')
    } else {
      console.log('[通知] sendNotify 模块未加载，无法发送通知')
    }
  } else if (NOTIFY_ALL) {
    let message = 'OpenList 存储状态正常\n'
    message += '==================================\n'
    message += `检测时间: ${now}\n`
    message += `OpenList: ${OPENLIST_URL}\n`
    message += `检测路径: ${MOUNT_PATHS.join(', ')}\n`
    message += `正常数量: ${normalCount}/${total}\n`
    message += '==================================\n\n'

    normalList.forEach((item, idx) => {
      message += `[${idx + 1}] ${item.mountPath} (${item.driver}) - ${item.probeResult}\n`
    })

    console.log('\n--- 通知内容 ---')
    console.log(message)

    if (notify) {
      await notify.sendNotify('OpenList 存储正常', message)
      console.log('[通知已发送]')
    }
  } else {
    console.log('\n所有存储状态正常，无需通知')
  }
}

// ============ 入口 ============
;(async () => {
  try {
    await main()
  } catch (e) {
    const now = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
    console.error(`\n[错误] ${e.message}`)

    if (notify) {
      let errMsg = 'OpenList 监控脚本运行失败\n'
      errMsg += '==================================\n'
      errMsg += `检测时间: ${now}\n`
      errMsg += `OpenList: ${OPENLIST_URL || '未配置'}\n`
      errMsg += '==================================\n\n'
      errMsg += `错误信息: ${e.message}\n\n`
      errMsg += '排查建议:\n'
      errMsg += '1. 检查 OPENLIST_URL 是否正确，OpenList 服务是否在线\n'
      errMsg += '2. 检查 OPENLIST_USERNAME / OPENLIST_PASSWORD 是否正确\n'
      errMsg += '3. 如使用 OPENLIST_TOKEN，确认 Token 未过期\n'
      errMsg += '4. 检查 OpenList 与青龙面板之间的网络连通性\n'

      await notify.sendNotify('OpenList 监控脚本异常', errMsg)
    }
  }
})()
