require("dotenv").config();
const express = require("express");
const cors = require("cors");
const morgan = require("morgan");
const bodyParser = require("body-parser");
const connectDB = require("./db"); // Import the connection function
const { connectToRabbitMQ } = require("./rabbitmq");

const app = express();
const PORT = process.env.PORT || 3000;
app.use(express.json({ limit: "10mb" }));
app.use(express.urlencoded({ extended: true, limit: "10mb" }));
// Connect to MongoDB
// connectDB();

// Connect to RabbitMQ
// connectToRabbitMQ();

// const function_router = require("./routers/function_router");
// const partner_router = require("./routers/partner_router");
// const top_up_router = require("./routers/topup_router");
const proxy_router = require("./routers/proxy_router");
const proxy_router_v2 = require("./routers/proxy_router_v2");

// Middleware untuk CORS
app.use(cors());

// Middleware untuk parsing JSON body

app.use(bodyParser.json());

// Logging middleware untuk monitoring traffic
// app.use(
//   morgan(
//     ":remote-addr - :method :url :status :res[content-length] - :response-time ms"
//   )
// );

app.use("/v1", proxy_router);
app.use("/v2", proxy_router_v2);
// app.use("/pub/functions", function_router);
// app.use("/pub/partners", partner_router);
// app.use("/internal/top-up", top_up_router);

app.listen(PORT, () => {
  console.log(`Proxy server berjalan di port ${PORT}`);
});
