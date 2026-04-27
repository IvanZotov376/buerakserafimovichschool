<?php
$dir = __DIR__ . '/photos';
$files = array_values(array_filter(scandir($dir), function($f) use ($dir) {
    $path = $dir . '/' . $f;
    return is_file($path) && preg_match('/\.(jpg|jpeg|png|gif|webp)$/i', $f);
}));

usort($files, function($a, $b) use ($dir) {
    return filemtime($dir . '/' . $b) - filemtime($dir . '/' . $a);
});

file_put_contents($dir . '/list.json', json_encode($files, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
echo 'list.json создан. Файлов: ' . count($files);