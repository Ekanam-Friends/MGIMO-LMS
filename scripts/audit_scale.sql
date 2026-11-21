-- Только чтение. Снимок времени и объём вымышленного конкурсного стенда.
-- Запуск: psql -v ON_ERROR_STOP=1 -U lms -d lms < scripts/audit_scale.sql
BEGIN TRANSACTION READ ONLY;
SELECT CURRENT_TIMESTAMP AS checked_at, pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT role, count(*) AS accounts FROM users GROUP BY role ORDER BY role;
SELECT count(*) AS students,
       count(*) FILTER (WHERE record_book LIKE 'ГЕН-%') AS generated_students,
       count(*) FILTER (WHERE study_group_id IS NULL) AS without_group
FROM students;
SELECT count(*) AS students_without_account
FROM students s WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.student_id = s.id AND u.role = 'student');
-- Наличие хэша не подтверждает известность пароля и успешный вход.
SELECT count(*) AS student_groups,
       count(*) FILTER (WHERE NOT EXISTS (
           SELECT 1 FROM lesson_groups lg JOIN lessons l ON l.id = lg.lesson_id
           WHERE lg.group_id = g.id AND l.status = 'active'
             AND l.date BETWEEN DATE '2026-09-01' AND DATE '2027-01-31'
       )) AS groups_without_semester_lessons
FROM study_groups g WHERE EXISTS (SELECT 1 FROM students s WHERE s.study_group_id = g.id);
SELECT count(*) AS teacher_accounts,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM lesson_teachers lt JOIN lessons l ON l.id = lt.lesson_id
           WHERE lt.teacher_id = u.teacher_id AND l.status = 'active'
             AND l.date BETWEEN DATE '2026-09-01' AND DATE '2027-01-31'
       )) AS with_imported_semester_lessons,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM courses c JOIN course_enrollments ce ON ce.course_id = c.id
           WHERE c.teacher_id = u.teacher_id AND c.academic_year = '2026/2027' AND c.status = 'open'
       )) AS with_current_open_ledger
FROM users u WHERE u.role = 'teacher';
-- Потенциальное покрытие: справочник преподавателей не равен числу входов.
SELECT count(DISTINCT lt.teacher_id) AS teachers_with_imported_semester_lessons
FROM lesson_teachers lt JOIN lessons l ON l.id = lt.lesson_id
WHERE l.status = 'active' AND l.date BETWEEN DATE '2026-09-01' AND DATE '2027-01-31';
SELECT id, started_at, finished_at, status, groups_total, groups_ok, groups_failed, lessons_seen
FROM import_runs ORDER BY id DESC LIMIT 5;
COMMIT;
