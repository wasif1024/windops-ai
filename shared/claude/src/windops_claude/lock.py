"""Record the Claude policy's hash for its version: make claude-policy-lock."""

from windops_claude.policy import lock

if __name__ == "__main__":
    print(f"Locked the Claude policy: {lock()}")
