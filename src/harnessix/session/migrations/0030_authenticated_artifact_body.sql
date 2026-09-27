-- 旧Artifact正文不因升级而获得来源认证；新Seal与正文同一行、同一事务提交。
ALTER TABLE agent_artifacts ADD COLUMN publication_seal BLOB DEFAULT NULL
    CHECK (publication_seal IS NULL OR length(publication_seal) BETWEEN 1 AND 4096);
