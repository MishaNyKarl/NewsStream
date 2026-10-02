"""Interactive setup: no password or API key in arguments, shell history or output."""
import argparse
import getpass
import hashlib

from argon2 import PasswordHasher

HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fingerprint', action='store_true')
    args = parser.parse_args()
    if args.fingerprint:
        key = getpass.getpass('LLM API key (только для локального отпечатка): ')
        if not key:
            raise SystemExit('Пустой ключ')
        print('sha256:' + hashlib.sha256(key.encode()).hexdigest()[:12])
        return
    password = getpass.getpass('Новый пароль администратора (не менее 14 символов): ')
    if len(password) < 14 or password != getpass.getpass('Повторите пароль: '):
        raise SystemExit('Пароли должны совпадать и содержать не менее 14 символов')
    print(HASHER.hash(password))


if __name__ == '__main__':
    main()
