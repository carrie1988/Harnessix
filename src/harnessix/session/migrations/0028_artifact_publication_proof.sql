-- 原有正文、Manifest与Session账本不回写；旧行无当前宿主保护证明。
ALTER TABLE agent_artifacts ADD COLUMN publication_epoch TEXT DEFAULT NULL
    CHECK (publication_epoch IS NULL OR length(publication_epoch) = 36);
ALTER TABLE agent_artifacts ADD COLUMN publication_policy TEXT DEFAULT NULL
    CHECK ((publication_epoch IS NULL AND publication_policy IS NULL)
        OR (publication_epoch IS NOT NULL AND publication_policy IS NOT NULL
            AND publication_policy = 'harnessix.public-output-protection/v1'));
