import base64
import hashlib

PAYLOAD = 'ePwiYZ*pH{Xk%exUvy<`Z)tXWXJKq+'
EXPECTED_SHA256 = '005b84989fa7ed6896a718feeeb2c0806ef1a777ecb339527333571ba8f2d35f'

def recover() -> str:
    encoded = base64.b85decode(PAYLOAD.encode()).decode()
    restored = encoded[::-1]
    return "".join(
        chr((ord(ch) - ord("A") - 5) % 26 + ord("A")) if "A" <= ch <= "Z" else ch
        for ch in restored
    )

def valid(candidate: str) -> bool:
    return hashlib.sha256(candidate.encode()).hexdigest() == EXPECTED_SHA256

if __name__ == "__main__":
    candidate = recover()
    print("candidate:", candidate)
    print("verified:", valid(candidate))
