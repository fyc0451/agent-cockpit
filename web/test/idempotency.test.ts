import { newIdempotencyKey } from '../api/idempotency'

const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

describe('idempotency key generation', () => {
  it('works when plain LAN HTTP does not expose crypto.randomUUID', () => {
    let next = 0
    vi.stubGlobal('crypto', {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.forEach((_byte, index) => {
          bytes[index] = next
          next += 1
        })
        return bytes
      },
    })

    expect(newIdempotencyKey()).toMatch(UUID_V4_RE)
  })
})
