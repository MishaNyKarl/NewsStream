import os
import uvicorn


if __name__ == '__main__':
    uvicorn.run('app.admin.web:create_app', factory=True, host=os.getenv('ADMIN_BIND', '127.0.0.1'),
                port=int(os.getenv('ADMIN_PORT', '8443')), workers=1, proxy_headers=False,
                ssl_certfile=os.environ['ADMIN_TLS_CERT'], ssl_keyfile=os.environ['ADMIN_TLS_KEY'],
                access_log=False, limit_concurrency=16, timeout_keep_alive=5)
