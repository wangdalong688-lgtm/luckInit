# 账号管理命令行手册（scripts/manage_accounts.py）

项目的账号与密码保存在当前目录的 SQLite 数据库 `cache.sqlite3` 中。

使用 `scripts/manage_accounts.py` 可以：

- 初始化账号表
- 创建账号
- 修改密码
- 禁用 / 启用账号
- 删除账号
- 验证账号密码

## 基本用法

查看帮助：

```bash
python scripts/manage_accounts.py -h
```

默认数据库文件：

- `./cache.sqlite3`

指定数据库文件：

```bash
python scripts/manage_accounts.py --db /path/to/cache.sqlite3 <command> ...
```

## 命令列表

### 1. 初始化账号表

```bash
python scripts/manage_accounts.py init
```

### 2. 查看所有用户

```bash
python scripts/manage_accounts.py list
```

### 3. 创建用户

交互输入密码：

```bash
python scripts/manage_accounts.py add alice
```

直接传密码：

```bash
python scripts/manage_accounts.py add alice --password "YourPassword"
```

### 4. 修改密码

```bash
python scripts/manage_accounts.py passwd alice
```

或：

```bash
python scripts/manage_accounts.py passwd alice --password "NewPassword"
```

### 5. 禁用用户

```bash
python scripts/manage_accounts.py disable alice
```

### 6. 启用用户

```bash
python scripts/manage_accounts.py enable alice
```

### 7. 删除用户

```bash
python scripts/manage_accounts.py delete alice
```

### 8. 验证用户密码

```bash
python scripts/manage_accounts.py verify alice
```

## 常见流程

### 新部署

```bash
python scripts/manage_accounts.py init
python scripts/manage_accounts.py add admin
python scripts/manage_accounts.py list
```

### 忘记密码

```bash
python scripts/manage_accounts.py passwd alice
```

### 暂时停用账号

```bash
python scripts/manage_accounts.py disable alice
python scripts/manage_accounts.py enable alice
```

## 安全建议

- 尽量使用交互输入密码，不要长期依赖 `--password`
- 定期修改管理员密码
- 生产环境建议配合 HTTPS 与反向代理
