drop trigger if exists skill_requires_improving_evaluation on skills.skill;
drop function if exists skills.require_improving_evaluation();
drop table if exists skills.loop_iteration;
drop table if exists skills.skill_evaluation;
drop table if exists skills.skill;
drop table if exists skills.learning_candidate;
drop table if exists skills.failure_occurrence;

delete from core.schema_migrations where version = 15;
