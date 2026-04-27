<?php
$dir = __DIR__; // текущая папка (photos)
$files = array_values(array_filter(scandir($dir), function($f) use ($dir) {
    $path = $dir . '/' . $f;
    return is_file($path) && preg_match('/\.(jpg|jpeg|png|gif|webp)$/i', $f);
}));

// Сортируем по времени изменения (сначала новые)
usort($files, function($a, $b) use ($dir) {
    return filemtime($dir . '/' . $b) - filemtime($dir . '/' . $a);
});

// Опционально: ограничение количества (например, 50)
$files = array_slice($files, 0, 50);

header('Content-Type: application/json');
echo json_encode($files);