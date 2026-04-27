const express = require('express');
const fetch = require('node-fetch');
const cors = require('cors');

const app = express();
app.use(cors());

// 👉 ВСТАВЬ СЮДА TOKEN
const TOKEN = "ВСТАВЬ_СЮДА_TOKEN";
const OWNER_ID = -215954534;

app.get('/photos', async (req, res) => {
  try {
    const response = await fetch(
      `https://api.vk.com/method/photos.get?owner_id=${OWNER_ID}&album_id=wall&count=50&access_token=${TOKEN}&v=5.131`
    );

    const data = await response.json();

    const photos = data.response.items.map(p => {
      const sizes = p.sizes;
      return sizes[sizes.length - 1].url;
    });

    res.json(photos);

  } catch (e) {
    res.status(500).json({ error: 'Ошибка сервера' });
  }
});

app.listen(3000, () => console.log("http://localhost:3000"));