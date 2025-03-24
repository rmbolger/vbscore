const matchId = window.location.pathname.split('/').pop();
const urlParams = new URLSearchParams(window.location.search);
const token = urlParams.get("token");

const ws = new WebSocket(`ws://${window.location.host}/ws/${matchId}?token=${token}`);

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);

    document.getElementById("matchTitle").innerText = `${data.teamA_name} vs. ${data.teamB_name}`;
    document.getElementById("teamA").innerText = data.score.teamA;
    document.getElementById("teamB").innerText = data.score.teamB;

    document.getElementById("teamA").style.background = data.teamA_color;
    document.getElementById("teamB").style.background = data.teamB_color;

    if (token) document.getElementById("controls").style.display = "block";
};

function updateScore(team, action) {
    ws.send(JSON.stringify({ team, action }));
}

function resetScore() {
    ws.send(JSON.stringify({ action: "reset" }));
}
