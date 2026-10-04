"""Demo photos (start/end position) for a subset of EXERCISE_TEMPLATES.

Sourced from free-exercise-db (github.com/yuhonas/free-exercise-db, MIT
licensed) and keyed by the exact template name from seed_data.EXERCISE_TEMPLATES,
since a forked exercise keeps that name verbatim (db.fork_exercise_from_template).
Templates with no reasonable match in that dataset are simply absent here.
"""

import os

MEDIA_DIR = os.path.join(os.path.dirname(__file__), "media", "exercises")

EXERCISE_IMAGE_SLUGS = {
    'Боковая планка': 'side_bridge',
    'Болгарские выпады': 'split_squat_with_dumbbells',
    'Велосипедные скручивания': 'air_bike',
    'Выпады в ходьбе со штангой': 'barbell_walking_lunge',
    'Выпады с гантелями': 'dumbbell_lunges',
    'Выпады со штангой': 'barbell_lunge',
    'Гак-присед': 'hack_squat',
    'Гиперэкстензия': 'hyperextensions_back_extensions',
    'Гудморнинг': 'good_morning',
    'Дровосек в кроссовере': 'standing_cable_wood_chop',
    'Жим Арнольда': 'arnold_dumbbell_press',
    'Жим Палоффа': 'pallof_press',
    'Жим в тренажёре': 'machine_bench_press',
    'Жим в тренажёре Хаммер': 'leverage_chest_press',
    'Жим в тренажёре на плечи': 'machine_shoulder_military_press',
    'Жим гантелей лёжа': 'dumbbell_bench_press',
    'Жим гантелей на наклонной скамье': 'incline_dumbbell_press',
    'Жим гантелей на наклонной скамье вниз головой': 'decline_dumbbell_bench_press',
    'Жим гантелей сидя': 'seated_dumbbell_press',
    'Жим гантелей стоя': 'standing_dumbbell_press',
    'Жим ногами': 'leg_press',
    'Жим носками в тренажёре': 'calf_press_on_the_leg_press_machine',
    'Жим штанги лёжа': 'barbell_bench_press_medium_grip',
    'Жим штанги на наклонной скамье': 'barbell_incline_bench_press_medium_grip',
    'Жим штанги на наклонной скамье вниз головой': 'decline_barbell_bench_press',
    'Жим штанги стоя': 'standing_military_press',
    'Жим штанги узким хватом лёжа': 'close_grip_barbell_bench_press',
    'Зашагивания на платформу': 'dumbbell_step_ups',
    'Колесо для пресса': 'ab_roller',
    'Концентрированный подъём на бицепс': 'concentration_curls',
    'Махи в кроссовере': 'standing_low_pulley_deltoid_raise',
    'Мёртвый жук': 'dead_bug',
    'Наклоны в сторону с гантелью': 'dumbbell_side_bend',
    'Обратная гиперэкстензия': 'reverse_hyperextension',
    'Обратные отжимания от скамьи': 'bench_dips',
    'Обратные разведения в тренажёре': 'reverse_machine_flyes',
    'Обратные скручивания': 'reverse_crunch',
    'Отведение ноги в кроссовере': 'glute_kickback',
    'Отжимания на брусьях': 'dips_chest_version',
    'Отжимания от пола': 'pushups',
    'Отжимания узким хватом': 'push_ups_close_triceps_position',
    'Планка': 'plank',
    'Подтягивания': 'pullups',
    'Подтягивания обратным хватом': 'chin_up',
    'Подъём EZ-штанги на бицепс': 'ez_bar_curl',
    'Подъём гантелей на бицепс': 'dumbbell_bicep_curl',
    'Подъём гантелей на бицепс молотом': 'hammer_curls',
    'Подъём гантелей на бицепс на наклонной скамье': 'incline_dumbbell_curl',
    'Подъём гантелей перед собой': 'front_dumbbell_raise',
    'Подъём коленей в висе': 'knee_hip_raise_on_parallel_bars',
    'Подъём на бицепс в тренажёре': 'machine_bicep_curl',
    'Подъём на бицепс на скамье Скотта': 'preacher_curl',
    'Подъём на носки сидя': 'seated_calf_raise',
    'Подъём на носки стоя': 'standing_calf_raises',
    'Подъём ног в висе': 'hanging_leg_raise',
    'Подъём ног лёжа': 'flat_bench_lying_leg_raise',
    'Подъём штанги на бицепс': 'barbell_curl',
    'Подъём штанги перед собой': 'standing_front_barbell_raise_over_head',
    'Пресс в тренажёре': 'ab_crunch_machine',
    'Присед в Смите': 'smith_machine_squat',
    'Присед с гирей у груди': 'goblet_squat',
    'Присед со штангой': 'barbell_squat',
    'Приседания без веса': 'bodyweight_squat',
    'Пулловер': 'straight_arm_dumbbell_pullover',
    'Пулловер на верхнем блоке': 'straight_arm_pulldown',
    'Разведение гантелей в наклоне': 'reverse_flyes',
    'Разведение гантелей в стороны': 'side_lateral_raise',
    'Разведение гантелей лёжа': 'dumbbell_flyes',
    'Разведение гантелей на наклонной скамье': 'incline_dumbbell_flyes',
    'Разведение ног в тренажёре': 'thigh_abductor',
    'Разгибание гантели за головой': 'standing_dumbbell_triceps_extension',
    'Разгибание запястий со штангой': 'seated_palms_down_barbell_wrist_curl',
    'Разгибание на трицепс в кроссовере одной рукой': 'cable_one_arm_tricep_extension',
    'Разгибание на трицепс из-за головы на блоке': 'cable_rope_overhead_triceps_extension',
    'Разгибание на трицепс на блоке': 'triceps_pushdown',
    'Разгибание на трицепс на блоке обратным хватом': 'reverse_grip_triceps_pushdown',
    'Разгибание на трицепс на блоке с канатом': 'triceps_pushdown_rope_attachment',
    'Разгибание ног в тренажёре': 'leg_extensions',
    'Разгибание руки с гантелью в наклоне': 'tricep_dumbbell_kickback',
    'Румынская тяга': 'romanian_deadlift',
    'Русские повороты': 'russian_twist',
    'Сведение ног в тренажёре': 'thigh_adductor',
    'Сведение рук в кроссовере': 'cable_crossover',
    'Сведение рук в тренажёре «бабочка»': 'butterfly',
    'Свинги с гирей одной рукой': 'one_arm_kettlebell_swings',
    'Сгибание запястий со штангой': 'seated_palm_up_barbell_wrist_curl',
    'Сгибание ног в тренажёре': 'lying_leg_curls',
    'Сгибание ног сидя в тренажёре': 'seated_leg_curl',
    'Сгибание ног стоя': 'standing_leg_curl',
    'Сгибание рук в блоке': 'standing_biceps_cable_curl',
    'Сгибание рук на бицепс обратным хватом': 'reverse_barbell_curl',
    'Сгибание рук на бицепс с канатом в кроссовере': 'cable_hammer_curls_rope_attachment',
    'Скручивания': 'crunches',
    'Скручивания в блоке': 'cable_crunch',
    'Скручивания на наклонной скамье': 'decline_crunch',
    'Становая тяга': 'barbell_deadlift',
    'Становая тяга сумо': 'sumo_deadlift',
    'Тяга Т-грифа': 't_bar_row_with_handle',
    'Тяга в тренажёре Хаммер': 'leverage_high_row',
    'Тяга верхнего блока': 'wide_grip_lat_pulldown',
    'Тяга верхнего блока обратным хватом': 'underhand_cable_pulldowns',
    'Тяга верхнего блока узким хватом': 'close_grip_front_lat_pulldown',
    'Тяга гантелей лёжа на наклонной скамье': 'dumbbell_incline_row',
    'Тяга гантели в наклоне': 'one_arm_dumbbell_row',
    'Тяга двух гантелей в наклоне': 'bent_over_two_dumbbell_row',
    'Тяга к лицу': 'face_pull',
    'Тяга на прямых ногах': 'stiff_legged_barbell_deadlift',
    'Тяга нижнего блока': 'seated_cable_rows',
    'Тяга с плинтов': 'rack_pulls',
    'Тяга штанги в наклоне': 'bent_over_barbell_row',
    'Тяга штанги в наклоне обратным хватом': 'reverse_grip_bent_over_rows',
    'Тяга штанги к подбородку': 'upright_barbell_row',
    'Французский жим': 'ez_bar_skullcrusher',
    'Французский жим с гантелями': 'lying_dumbbell_tricep_extension',
    'Фронтальный присед': 'front_barbell_squat',
    'Хип-траст со штангой': 'barbell_hip_thrust',
    'Шраги с гантелями': 'dumbbell_shrug',
    'Шраги со штангой': 'barbell_shrug',
    'Ягодичный мостик': 'butt_lift_bridge',
    'Ягодичный мостик со штангой': 'barbell_glute_bridge',
    'Жим на блоках сидя': 'cable_chest_press',
    'Жим на блоках на наклонной скамье': 'incline_cable_chest_press',
    'Жим лёжа в Смите': 'smith_machine_bench_press',
    'Жим в Смите на наклонной скамье': 'smith_machine_incline_bench_press',
    'Жим штанги лёжа широким хватом': 'wide_grip_barbell_bench_press',
    'Жим гантелей лёжа нейтральным хватом': 'dumbbell_bench_press_with_neutral_grip',
    'Жим гантелей на наклонной скамье нейтральным хватом': 'hammer_grip_incline_db_bench_press',
    'Жим в тренажёре на наклонной скамье': 'leverage_incline_chest_press',
    'Жим в тренажёре вниз головой': 'leverage_decline_chest_press',
    'Разведение рук на блоках лёжа': 'flat_bench_cable_flyes',
    'Сведение рук в кроссовере снизу вверх': 'low_cable_crossover',
    'Разведение рук на блоках на наклонной скамье': 'incline_cable_flye',
    'Отжимания с ногами на возвышении': 'push_ups_with_feet_elevated',
    'Разгибание на трицепс на блоке с V-рукояткой': 'triceps_pushdown_v_bar_attachment',
    'Жим гантелей узким хватом лёжа': 'close_grip_dumbbell_press',
    'Жим узким хватом лёжа в Смите': 'smith_machine_close_grip_bench_press',
    'Джей-эм жим': 'jm_press',
    'Отжимания на брусьях в тренажёре': 'dip_machine',
    'Отжимания на брусьях на трицепс': 'dips_triceps_version',
    'Французский жим EZ-грифом на наклонной скамье вниз головой': 'decline_ez_bar_triceps_extension',
    'Французский жим на наклонной скамье': 'incline_barbell_triceps_extension',
    'Разгибание штанги из-за головы стоя': 'standing_overhead_barbell_triceps_extension',
    'Французский жим на блоке лёжа': 'cable_lying_triceps_extension',
    'Разгибание на трицепс в тренажёре': 'machine_triceps_extension',
    'Тейт-пресс': 'tate_press',
    'Подъём гантелей молотом поочерёдно': 'alternate_hammer_curl',
    'Подъём гантелей молотом через корпус': 'cross_body_hammer_curl',
    'Сгибание рук на скамье Скотта в блоке': 'cable_preacher_curl',
    'Подъём EZ-штанги на бицепс узким хватом': 'close_grip_ez_bar_curl',
    'Подъём штанги на бицепс широким хватом': 'wide_grip_standing_barbell_curl',
    'Подъём гантелей на бицепс сидя': 'seated_dumbbell_curl',
    'Подъём гантелей молотом на наклонной скамье': 'incline_hammer_curls',
    'Сгибания Зоттмана': 'zottman_curl',
    'Сгибания «паук» с EZ-грифом': 'spider_curl',
    'Сгибание рук в тренажёре Скотта': 'machine_preacher_curls',
    'Сгибание руки в кроссовере стоя': 'standing_one_arm_cable_curl',
    'Подъём гантели на бицепс на скамье Скотта одной рукой': 'one_arm_dumbbell_preacher_curl',
    'Жим штанги сидя': 'seated_barbell_military_press',
    'Жим гантели одной рукой': 'dumbbell_one_arm_shoulder_press',
    'Жим в Смите стоя': 'smith_machine_overhead_shoulder_press',
    'Жим гантелей стоя поочерёдно': 'standing_alternating_dumbbell_press',
    'Жим гантелей стоя нейтральным хватом': 'standing_palms_in_dumbbell_press',
    'Жим на блоке сидя': 'seated_cable_shoulder_press',
    'Разведение гантелей в стороны сидя': 'seated_side_lateral_raise',
    'Разведение гантели в сторону одной рукой': 'one_arm_side_laterals',
    'Обратное разведение в кроссовере': 'cable_rear_delt_fly',
    'Разведение гантелей в наклоне сидя': 'seated_bent_over_rear_delt_raise',
    'Подъём руки перед собой на блоке': 'front_cable_raise',
    'Жим штанги из-за головы стоя': 'standing_barbell_press_behind_neck',
    'Тяга гантелей к подбородку': 'standing_dumbbell_upright_row',
    'Тяга к подбородку на блоке': 'upright_cable_row',
    'Тяга верхнего блока V-рукояткой': 'v_bar_pulldown',
    'Тяга верхнего блока одной рукой': 'one_arm_lat_pulldown',
    'Тяга в Смите в наклоне': 'smith_machine_bent_over_row',
    'Тяга Т-грифа лёжа на груди': 'lying_t_bar_row',
    'Тяга нижнего блока одной рукой': 'seated_one_arm_cable_pulley_rows',
    'Тяга в рычажном тренажёре': 'leverage_iso_row',
    'Тяга блока на коленях': 'kneeling_high_pulley_row',
    'Тяга гантелей в наклоне нейтральным хватом': 'bent_over_two_dumbbell_row_with_palms_in',
    'Тяга штанги лёжа на наклонной скамье': 'incline_bench_pull',
    'Подтягивания параллельным хватом': 'v_bar_pullup',
    'Тяга штанги лёжа на скамье': 'straight_bar_bench_mid_rows',
    'Становая тяга с дефицитом': 'deficit_deadlift',
    'Присед в полную амплитуду со штангой': 'barbell_full_squat',
    'Присед на ящик со штангой': 'box_squat',
    'Присед с гантелями': 'dumbbell_squat',
    'Присед с широкой постановкой ног': 'wide_stance_barbell_squat',
    'Присед с узкой постановкой ног': 'narrow_stance_squats',
    'Гак-присед со штангой за спиной': 'barbell_hack_squat',
    'Жим ногами узкой постановкой': 'narrow_stance_leg_press',
    'Обратные выпады с гантелями': 'dumbbell_rear_lunge',
    'Зашагивания на платформу со штангой': 'barbell_step_ups',
    'Сплит-присед в Смите': 'smith_single_leg_split_squat',
    'Плие-присед с гантелью': 'plie_dumbbell_squat',
    'Присед с гантелями на скамью': 'dumbbell_squat_to_a_bench',
    'Тяга на прямых ногах с гантелями': 'stiff_legged_dumbbell_deadlift',
    'Тяга на прямых ногах в Смите': 'smith_machine_stiff_legged_deadlift',
    'Подъём корпуса в глют-хам тренажёре': 'glute_ham_raise',
    'Ягодичный мостик на одной ноге': 'single_leg_glute_bridge',
    'Тяга каната между ног на блоке': 'pull_through',
    'Подъём на носки со штангой стоя': 'standing_barbell_calf_raise',
    'Подъём на носки с гантелями стоя': 'standing_dumbbell_calf_raise',
    'Подъём на носки в Смите': 'smith_machine_calf_raise',
    'Подъём на носки сидя со штангой': 'barbell_seated_calf_raise',
    'Разгибание одной ноги в тренажёре': 'single_leg_leg_extension',
    'Подъём корпуса из положения лёжа': 'sit_up',
    'Скручивания сидя на блоке': 'cable_seated_crunch',
    'Перекрёстные скручивания': 'cross_body_crunch',
    'Скручивания на косые мышцы на полу': 'oblique_crunches_on_the_floor',
    'Скручивания на косые на наклонной скамье': 'decline_oblique_crunch',
    'Подтягивание коленей к груди лёжа': 'flat_bench_leg_pull_in',
    'Наклоны в сторону со штангой': 'barbell_side_bend',
    'Наклоны в сторону на блоке одной рукой': 'one_arm_high_pulley_cable_side_bends',
    'Шраги на блоке': 'cable_shrugs',
    'Шраги со штангой за спиной': 'barbell_shrug_behind_the_back',
    'Шраги в рычажном тренажёре': 'leverage_shrug',
    'Сгибание запястий с гантелью сидя': 'seated_dumbbell_palms_up_wrist_curl',
    'Разгибание запястий с гантелью сидя': 'seated_dumbbell_palms_down_wrist_curl',
    'Сгибание запястий на блоке': 'cable_wrist_curl',
    'Жим штанги лёжа на полу': 'floor_press',
    'Жим в Смите вниз головой': 'decline_smith_press',
    'Жим гантели лёжа одной рукой': 'one_arm_dumbbell_bench_press',
    'Разведение гантелей лёжа вниз головой': 'decline_dumbbell_flyes',
    'Разведение гантели лёжа одной рукой': 'one_arm_flat_bench_dumbbell_flye',
    'Сведение рук в кроссовере одной рукой': 'single_arm_cable_crossover',
    'Жим на блоке стоя': 'standing_cable_chest_press',
    'Жим штанги вниз головой широким хватом': 'wide_grip_decline_barbell_bench_press',
    'Отжимания от возвышения': 'incline_push_up',
    'Отжимания широким хватом': 'push_up_wide',
    'Отжимания на петлях': 'suspended_push_up',
    'Жим блина Свенда': 'svend_press',
    'Жим штанги лёжа с упоров': 'pin_presses',
    'Круги гантелями лёжа': 'around_the_worlds',
    'Разгибание на блоке на наклонной скамье': 'cable_incline_triceps_extension',
    'Французский жим гантелей вниз головой': 'decline_dumbbell_triceps_extension',
    'Разгибание гантели из-за головы одной рукой': 'dumbbell_one_arm_triceps_extension',
    'Разгибание на блоке стоя на коленях': 'kneeling_cable_triceps_extension',
    'Разгибание на нижнем блоке': 'low_cable_triceps_extension',
    'Разгибание одной рукой на нижнем блоке': 'standing_low_pulley_one_arm_triceps_extension',
    'Разгибание гантелей в наклоне сидя': 'seated_bent_over_two_arm_dumbbell_triceps_extension',
    'Жим EZ-грифа узким хватом лёжа': 'close_grip_ez_bar_press',
    'Французский жим с весом тела': 'body_tricep_press',
    'Жим штанги обратным хватом лёжа': 'reverse_triceps_bench_press',
    'Отжимания узким хватом на гантели': 'close_grip_push_up_off_of_a_dumbbell',
    'Подъём гантелей на наклонной скамье поочерёдно': 'alternate_incline_dumbbell_curl',
    'Концентрированный подъём стоя': 'standing_concentration_curl',
    'Сгибание рук на блоке лёжа': 'lying_cable_curl',
    'Сгибание рук на блоках над головой': 'overhead_cable_curl',
    'Сгибание рук обратным хватом на блоке': 'reverse_cable_curl',
    'Подъём EZ-штанги обратным хватом на скамье Скотта': 'reverse_barbell_preacher_curls',
    'Подъём гантелей обратным хватом': 'standing_dumbbell_reverse_curl',
    'Молот на скамье Скотта': 'preacher_hammer_dumbbell_curl',
    'Подъём гантелей на скамье Скотта двумя руками': 'two_arm_dumbbell_preacher_curl',
    'Подъём штанги на бицепс узким хватом': 'close_grip_standing_barbell_curl',
    'Подъём штанги вдоль корпуса': 'drag_curl',
    'Подъём гантелей лёжа на наклонной скамье грудью': 'dumbbell_prone_incline_curl',
    'Жим на блоке стоя на плечи': 'cable_shoulder_press',
    'Разведение на блоке сидя': 'cable_seated_lateral_raise',
    'Разведение в наклоне на нижнем блоке': 'bent_over_low_pulley_side_lateral',
    'Разведение гантелей лёжа на заднюю дельту': 'lying_rear_delt_raise',
    'Разведение гантелей в наклоне с упором головы на скамью': 'bent_over_dumbbell_rear_delt_raise_with_head_on_bench',
    'Разведение гантели лёжа на боку': 'lying_one_arm_lateral_raise',
    'Подъём блина перед собой': 'front_plate_raise',
    'Тяга штанги в наклоне на заднюю дельту': 'barbell_rear_delt_row',
    'Тяга каната на заднюю дельту': 'cable_rope_rear_delt_rows',
    'Наружная ротация плеча с гантелью': 'external_rotation',
    'Наружная ротация плеча на блоке': 'external_rotation_with_cable',
    'Кубинский жим': 'cuban_press',
    'Подъём гантелей в плоскости лопатки': 'dumbbell_scaption',
    'Тяга нижнего блока с возвышения': 'elevated_cable_rows',
    'Тяга верхнего блока одной рукой на коленях': 'kneeling_single_arm_high_pulley_row',
    'Тяга блока «шотган» одной рукой': 'shotgun_row',
    'Тяга верхнего блока в полной амплитуде': 'full_range_of_motion_lat_pulldown',
    'Тяга верхнего блока за голову': 'wide_grip_pulldown_behind_the_neck',
    'Австралийские подтягивания': 'inverted_row',
    'Тяга на петлях': 'suspended_row',
    'Подтягивания с весом': 'weighted_pull_ups',
    'Подтягивания с резинкой': 'band_assisted_pull_up',
    'Лопаточные подтягивания': 'scapular_pull_up',
    'Пулловер со штангой': 'bent_arm_barbell_pullover',
    'Становая тяга с трэп-грифом': 'trap_bar_deadlift',
    'Гак-присед в тренажёре узкой постановкой': 'narrow_stance_hack_squats',
    'Становая тяга на блоке': 'cable_deadlifts',
    'Обратные выпады со штангой с платформы': 'elevated_back_lunge',
    'Выпады в ходьбе без веса': 'bodyweight_walking_lunge',
    'Зашагивание с подъёмом колена': 'step_up_with_knee_raise',
    'Подъём на носки сидя с гантелью одной ногой': 'dumbbell_seated_one_leg_calf_raise',
    'Подъём на носки «ослик»': 'donkey_calf_raises',
    'Глют-хам рейз на скамье без тренажёра': 'natural_glute_ham_raise',
    'Румынская тяга на одной ноге с гирей': 'kettlebell_one_legged_deadlift',
    'Приведение ноги на блоке': 'cable_hip_adduction',
    'Присед лёжа в тренажёре': 'lying_machine_squat',
    'Роллаут со штангой': 'barbell_ab_rollout',
    'Обратные скручивания на наклонной скамье': 'decline_reverse_crunch',
    'Подъём таза лёжа с согнутыми коленями': 'bent_knee_hip_raise',
    'Скручивания с руками над головой': 'crunch_hands_overhead',
    'Обратные скручивания на блоке': 'cable_reverse_crunch',
    'Скручивания с канатом на блоке': 'rope_crunch',
    'Скручивания с канатом стоя': 'standing_rope_crunch',
    'Складной нож лёжа': 'jackknife_sit_up',
    'Русские повороты на блоке': 'cable_russian_twists',
    'Скручивания на косые': 'oblique_crunches',
    'Повороты со штангой сидя': 'seated_barbell_twist',
    'Касания пяток лёжа': 'alternate_heel_touchers',
    'Подъём каната по диагонали на блоке': 'standing_cable_lift',
    'Повороты с блином': 'plate_twist',
    'Шраги в Смите за спиной': 'smith_machine_behind_the_back_shrug',
    'Шраги в тренажёре для икр': 'calf_machine_shoulder_shrug',
    'Сгибание пальцев со штангой': 'finger_curls',
    'Сгибание запястий с гантелью над скамьёй': 'palms_up_dumbbell_wrist_curl_over_a_bench',
    'Разгибание запястий с гантелью над скамьёй': 'palms_down_dumbbell_wrist_curl_over_a_bench',
    'Сгибание запястий со штангой за спиной': 'standing_palms_up_barbell_behind_the_back_wrist_curl',
    'Ролл для запястий': 'wrist_roller',
    'Удержание блинов щипком': 'plate_pinch',
    'Пронация с гантелью лёжа': 'dumbbell_lying_pronation',
    'Супинация с гантелью лёжа': 'dumbbell_lying_supination',
    'Шея с блином лёжа лицом вниз': 'lying_face_down_plate_neck_resistance',
    'Шея с блином лёжа лицом вверх': 'lying_face_up_plate_neck_resistance',
    'Жим гантелей лёжа на полу': 'dumbbell_floor_press',
    'Гиперэкстензия на полу': 'hyperextensions_with_no_hyperextension_bench',
}


def catalog_key(ex) -> str:
    """The name this exercise's catalog assets are filed under.

    `original_name`, not `name`: a fork keeps the template's name at creation,
    but the user is invited to rename it ("✏️ Название", right next to the
    description and photo buttons) — and keying on the mutable field meant the
    rename silently stripped both. `original_name` is written once at creation
    and never touched again, which is exactly what a lookup key needs.

    Falls back to `name` for a row that predates the column or was fetched by
    a query that didn't select it; those are the rows for which the two are
    equal anyway.
    """
    try:
        original = ex["original_name"]
    except (IndexError, KeyError):
        original = None
    return original or ex["name"]


def get_images(exercise_name: str) -> list[str]:
    """Absolute paths to the [start, end] position photos, or [] if none exist."""
    slug = EXERCISE_IMAGE_SLUGS.get(exercise_name)
    if slug is None:
        return []
    paths = [os.path.join(MEDIA_DIR, f"{slug}_1.jpg"), os.path.join(MEDIA_DIR, f"{slug}_2.jpg")]
    return paths if all(os.path.exists(p) for p in paths) else []


def get_images_for(ex) -> list[str]:
    """get_images for an exercise row, keyed the rename-proof way."""
    return get_images(catalog_key(ex))


def media_url(path: str) -> str:
    """Абсолютный путь диска -> относительный URL раздачи /media/exercises/<name>
    (маршрут api_v1_media.get_media_file). Живёт здесь, а не в api_v1_media:
    тот же URL нужен сериализатору упражнения в api_v1_common, а api_v1_media
    сам импортирует api_v1_common — оттуда был бы круговой импорт."""
    return f"/media/exercises/{os.path.basename(path)}"


def thumb_url_for(ex) -> str | None:
    """Миниатюра строки списка упражнений — первый кадр каталога в том же
    порядке, что GET /exercises/{id}/media отдаёт `images`, чтобы превью в
    списке совпадало с первым кадром карточки. None — каталожных кадров нет
    (своё упражнение, шаблон без пары фото).

    Дёшево на 150+ строк: ни сети, ни базы — поиск слага в словаре и две
    проверки файла на локальном диске (get_images), а у своего упражнения
    без слага — только поиск в словаре. Своё фото атлета сюда не попадает
    намеренно: это приватные байты за токеном (GET /exercises/{id}/photo), а
    не публичный URL; о нём сообщает отдельный флаг `has_photo`."""
    images = get_images_for(ex)
    return media_url(images[0]) if images else None


# Зацикленная демонстрация повтора: тот же тренер, что в пушах, вместо
# случайного человека из открытой базы (TONE_OF_VOICE.md, «Стиль картинок» —
# персонаж один на весь продукт). Собирается офлайн, scripts/gen_exercise_demos.py;
# бот только отдаёт готовый файл. Клипы появляются по одному, поэтому это не
# замена фото, а верхняя ступень: есть клип — идёт клип, нет — прежняя пара
# кадров, и покрытие не проседает ни на одном шаге раскатки.
def get_animation(exercise_name: str) -> str | None:
    """Путь к зацикленному клипу упражнения, или None если его ещё не сняли."""
    slug = EXERCISE_IMAGE_SLUGS.get(exercise_name)
    if slug is None:
        return None
    path = os.path.join(MEDIA_DIR, f"{slug}_demo.mp4")
    return path if os.path.exists(path) else None


def get_animation_for(ex) -> str | None:
    """get_animation for an exercise row, keyed the rename-proof way."""
    return get_animation(catalog_key(ex))


# Telegram hands back a file_id for every uploaded photo, and re-sending that id
# costs no upload at all. The live tracker shows the same photos over and over
# (every exercise, every workout, every user), so remembering the ids for the
# process lifetime turns all but the first send into a plain string.
_FILE_IDS: dict[str, str] = {}


def cached_file_id(path: str) -> str | None:
    return _FILE_IDS.get(path)


def remember_file_id(path: str, file_id: str | None) -> None:
    if file_id:
        _FILE_IDS[path] = file_id
