-- 本地单机部署：在同一个 postgres 实例里建出 A / B 两个模拟业务库
--
-- 为什么这样建：docker-compose.yml 的口径是「A/B 库收编进 EKOS 的 ekos-postgres
-- （宿主 15432，库 test / retail）」，wren-docker*/workspace/connection_info.json 也
-- 正是 host.docker.internal:15432 + 库 test / retail。本机没有 EKOS，故用仓库自带的
-- 建表脚本在同一实例里还原这套拓扑，不改动任何仓库文件。
--
-- 执行者：postgres 官方镜像的 initdb 钩子（仅首次初始化空数据卷时执行一次）。
--   /seed-a = ./wren-docker ，/seed-b = ./wren-docker-b（只读挂载）

CREATE DATABASE retail;

-- 模拟库 A · 电商域（6 表）
\c test
\i /seed-a/postgres/init/01-sample.sql
\i /seed-a/postgres/init/02-ecommerce.sql

-- 模拟库 B · 零售会员域（8 表）
\c retail
\i /seed-b/postgres/init/01-retail.sql
