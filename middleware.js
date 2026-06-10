export const config = {
  matcher: ['/(.*\\.db)'],
}

async function verifyJWT(token, secret) {
  const [headerB64, payloadB64, signatureB64] = token.split('.')
  if (!headerB64 || !payloadB64 || !signatureB64) return null

  const enc = new TextEncoder()
  const keyData = enc.encode(secret)
  const cryptoKey = await crypto.subtle.importKey(
    'raw', keyData, { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']
  )

  const data = enc.encode(`${headerB64}.${payloadB64}`)
  const signature = Uint8Array.from(
    atob(signatureB64.replace(/-/g, '+').replace(/_/g, '/')),
    c => c.charCodeAt(0)
  )

  const valid = await crypto.subtle.verify('HMAC', cryptoKey, signature, data)
  if (!valid) return null

  return JSON.parse(atob(payloadB64.replace(/-/g, '+').replace(/_/g, '/')))
}

export default async function middleware(request) {
  const cookieHeader = request.headers.get('cookie') || ''
  const match = cookieHeader.match(/(?:^|;\s*)ibba_auth=([^;]+)/)
  const token = match ? decodeURIComponent(match[1]) : null

  if (!token) {
    return new Response('Unauthorized — please log in at the portal.', {
      status: 401,
      headers: { 'Content-Type': 'text/plain' },
    })
  }

  const secret = process.env.SUPABASE_JWT_SECRET
  if (!secret) {
    return new Response('Server misconfiguration', { status: 500 })
  }

  try {
    const payload = await verifyJWT(token, secret)
    if (!payload) {
      return new Response('Unauthorized — invalid token.', {
        status: 401,
        headers: { 'Content-Type': 'text/plain' },
      })
    }
    if (payload.exp && Date.now() / 1000 > payload.exp) {
      return new Response('Session expired — please log in again.', {
        status: 401,
        headers: { 'Content-Type': 'text/plain' },
      })
    }
  } catch {
    return new Response('Unauthorized', { status: 401 })
  }
}
