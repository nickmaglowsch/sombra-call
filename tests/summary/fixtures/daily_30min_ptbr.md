# Reunião: planejamento sprint 42 (sintética)

participantes: EU (Nick), OUTROS

[14:00:00] OUTROS: bom dia pessoal, vamos começar o planejamento da sprint quarenta e dois
[14:00:08] OUTROS: certo, certo
[14:00:17] EU: acho que é o meu microfone, peraí
[14:00:23] EU: tá
[14:00:35] OUTROS: só um segundo que meu áudio tá cortando
[14:00:41] OUTROS: sim
[14:00:45] OUTROS: a pauta hoje é o roadmap do Q4, o bug do checkout e a migração do banco
[14:00:51] OUTROS: só um segundo que meu áudio tá cortando
[14:00:58] OUTROS: uhum
[14:01:04] OUTROS: pode ser
[14:01:13] EU: tá
[14:01:20] EU: tá
[14:01:30] EU: beleza, eu trouxe os números do checkout da semana passada
[14:01:39] OUTROS: uhum
[14:01:51] OUTROS: alguém tá ouvindo eco?
[14:01:57] OUTROS: deixa eu ver aqui
[14:02:08] EU: acho que é o meu microfone, peraí
[14:02:15] TELA f0001 "Zoom - Roadmap Q4"
[14:02:21] OUTROS: alguém tá ouvindo eco?
[14:02:31] OUTROS: pode ser
[14:02:37] OUTROS: deixa eu ver aqui
[14:02:43] OUTROS: só um segundo que meu áudio tá cortando
[14:02:55] OUTROS: certo, certo
[14:03:00] OUTROS: então primeiro o roadmap, a gente tem três épicos grandes pro trimestre
[14:03:09] OUTROS: certo, certo
[14:03:19] EU: tá
[14:03:29] EU: entendi
[14:03:39] EU: acho que é o meu microfone, peraí
[14:03:45] OUTROS: o app mobile novo, o painel de relatórios e a integração com o ERP
[14:03:51] OUTROS: alguém tá ouvindo eco?
[14:04:01] EU: acho que é o meu microfone, peraí
[14:04:08] OUTROS: sim
[14:04:14] OUTROS: só um segundo que meu áudio tá cortando
[14:04:25] EU: tá
[14:04:30] EU: acho que o painel de relatórios é o que mais tem pedido dos clientes
[14:04:36] OUTROS: alguém tá ouvindo eco?
[14:04:43] EU: faz sentido
[14:04:54] OUTROS: só um segundo que meu áudio tá cortando
[14:05:03] OUTROS: sim
[14:05:15] OUTROS: concordo, o comercial pediu isso em quase toda reunião do último mês
[14:05:25] EU: faz sentido
[14:05:33] EU: entendi
[14:05:40] OUTROS: certo, certo
[14:05:51] OUTROS: deixa eu ver aqui
[14:06:00] OUTROS: mas o app mobile já tem design pronto, seria desperdício parar agora
[14:06:10] EU: entendi
[14:06:20] EU: faz sentido
[14:06:28] OUTROS: voltou, pode continuar
[14:06:37] EU: entendi
[14:06:45] EU: dá pra fazer os dois se a gente segurar a integração com o ERP pra janeiro
[14:06:51] EU: tá
[14:07:01] OUTROS: pode ser
[14:07:08] OUTROS: sim
[14:07:15] EU: faz sentido
[14:07:24] OUTROS: uhum
[14:07:30] OUTROS: faz sentido, o cliente do ERP só assina contrato em dezembro mesmo
[14:07:36] OUTROS: só um segundo que meu áudio tá cortando
[14:07:46] OUTROS: sim
[14:07:54] OUTROS: voltou, pode continuar
[14:08:02] OUTROS: alguém tá ouvindo eco?
[14:08:11] OUTROS: alguém tá ouvindo eco?
[14:08:15] OUTROS: então fica decidido, ERP vai pro Q1 e a gente foca em mobile e relatórios
[14:08:24] EU: tá
[14:08:36] EU: tá
[14:08:44] EU: faz sentido
[14:08:55] EU: acho que é o meu microfone, peraí
[14:09:00] EU: fechado, eu atualizo o roadmap no Notion até amanhã
[14:09:06] OUTROS: voltou, pode continuar
[14:09:17] EU: entendi
[14:09:28] OUTROS: alguém tá ouvindo eco?
[14:09:39] EU: faz sentido
[14:09:45] OUTROS: Nick, você consegue estimar o painel de relatórios até sexta?
[14:09:56] OUTROS: pode ser
[14:10:07] OUTROS: sim
[14:10:13] EU: faz sentido
[14:10:21] OUTROS: certo, certo
[14:10:30] EU: consigo sim, sexta eu mando a estimativa com as histórias quebradas
[14:10:36] EU: faz sentido
[14:10:42] OUTROS: deixa eu ver aqui
[14:10:54] EU: entendi
[14:11:01] OUTROS: voltou, pode continuar
[14:11:08] OUTROS: pode ser
[14:11:15] TELA f0002 "Grafana - Checkout errors"
[14:11:27] EU: faz sentido
[14:11:33] OUTROS: certo, certo
[14:11:42] OUTROS: pode ser
[14:11:52] EU: entendi
[14:12:00] OUTROS: agora o bug do checkout, a taxa de erro subiu pra quatro por cento
[14:12:12] OUTROS: pode ser
[14:12:24] OUTROS: só um segundo que meu áudio tá cortando
[14:12:32] OUTROS: voltou, pode continuar
[14:12:41] OUTROS: sim
[14:12:45] EU: olhei os logs, é timeout no gateway de pagamento quando o carrinho tem cupom
[14:12:54] OUTROS: deixa eu ver aqui
[14:13:01] EU: tá
[14:13:08] OUTROS: certo, certo
[14:13:15] EU: acho que é o meu microfone, peraí
[14:13:22] OUTROS: uhum
[14:13:30] OUTROS: isso começou depois do deploy de terça, né?
[14:13:42] OUTROS: alguém tá ouvindo eco?
[14:13:49] EU: entendi
[14:13:57] OUTROS: uhum
[14:14:04] OUTROS: pode ser
[14:14:15] EU: isso, o deploy de terça mudou o cálculo do desconto e ficou lento
[14:14:23] OUTROS: alguém tá ouvindo eco?
[14:14:33] OUTROS: sim
[14:14:40] OUTROS: voltou, pode continuar
[14:14:52] OUTROS: só um segundo que meu áudio tá cortando
[14:15:00] OUTROS: a Marina pode pegar esse bug, ela conhece o módulo de cupom
[14:15:11] EU: acho que é o meu microfone, peraí
[14:15:22] OUTROS: uhum
[14:15:31] EU: acho que é o meu microfone, peraí
[14:15:45] OUTROS: Marina aqui, pego sim, consigo corrigir até quarta que vem
[14:15:55] OUTROS: pode ser
[14:16:04] OUTROS: pode ser
[14:16:13] EU: tá
[14:16:22] EU: acho que é o meu microfone, peraí
[14:16:30] OUTROS: ótimo, e enquanto isso a gente volta o feature flag do desconto novo?
[14:16:36] OUTROS: deixa eu ver aqui
[14:16:42] OUTROS: deixa eu ver aqui
[14:16:51] OUTROS: certo, certo
[14:16:57] OUTROS: sim
[14:17:07] OUTROS: uhum
[14:17:15] EU: sim, eu desligo o feature flag hoje depois da reunião
[14:17:21] OUTROS: alguém tá ouvindo eco?
[14:17:28] OUTROS: só um segundo que meu áudio tá cortando
[14:17:34] OUTROS: sim
[14:17:44] OUTROS: uhum
[14:17:50] OUTROS: deixa eu ver aqui
[14:18:00] OUTROS: decidido então, flag desligado hoje e correção da Marina até quarta
[14:18:09] OUTROS: certo, certo
[14:18:20] EU: entendi
[14:18:28] OUTROS: alguém tá ouvindo eco?
[14:18:36] EU: faz sentido
[14:18:45] TELA f0003 "Confluence - Migração Postgres 16"
[14:18:51] EU: faz sentido
[14:19:00] EU: faz sentido
[14:19:09] EU: entendi
[14:19:15] OUTROS: certo, certo
[14:19:21] OUTROS: voltou, pode continuar
[14:19:30] OUTROS: último ponto, a migração do banco pro Postgres dezesseis
[14:19:41] EU: entendi
[14:19:50] OUTROS: voltou, pode continuar
[14:19:57] OUTROS: só um segundo que meu áudio tá cortando
[14:20:03] OUTROS: deixa eu ver aqui
[14:20:15] OUTROS: o Rafael fez o teste em staging e deu tudo certo, só a janela que falta
[14:20:23] OUTROS: certo, certo
[14:20:34] OUTROS: só um segundo que meu áudio tá cortando
[14:20:40] OUTROS: só um segundo que meu áudio tá cortando
[14:20:48] EU: acho que é o meu microfone, peraí
[14:21:00] OUTROS: Rafael, qual janela você sugere?
[14:21:06] OUTROS: voltou, pode continuar
[14:21:18] EU: entendi
[14:21:28] OUTROS: sim
[14:21:35] OUTROS: sim
[14:21:45] OUTROS: eu sugiro sábado de madrugada, das duas às cinco
[14:21:52] OUTROS: só um segundo que meu áudio tá cortando
[14:22:02] OUTROS: só um segundo que meu áudio tá cortando
[14:22:10] EU: acho que é o meu microfone, peraí
[14:22:17] OUTROS: alguém tá ouvindo eco?
[14:22:30] EU: tem que avisar o suporte com antecedência por causa do downtime
[14:22:42] OUTROS: deixa eu ver aqui
[14:22:54] OUTROS: deixa eu ver aqui
[14:23:06] OUTROS: pode ser
[14:23:15] OUTROS: quem avisa o suporte?
[14:23:27] OUTROS: deixa eu ver aqui
[14:23:34] OUTROS: só um segundo que meu áudio tá cortando
[14:23:43] OUTROS: sim
[14:23:54] OUTROS: uhum
[14:24:00] OUTROS: eu aviso, o Rafael manda o comunicado pro suporte até quinta
[14:24:12] EU: entendi
[14:24:21] EU: entendi
[14:24:28] OUTROS: voltou, pode continuar
[14:24:38] OUTROS: sim
[14:24:45] OUTROS: e ainda não sabemos se o backup vai caber no disco novo, alguém verifica?
[14:24:57] OUTROS: voltou, pode continuar
[14:25:05] OUTROS: sim
[14:25:11] OUTROS: deixa eu ver aqui
[14:25:17] OUTROS: deixa eu ver aqui
[14:25:26] OUTROS: deixa eu ver aqui
[14:25:30] EU: não sei se o volume de backup cabe, precisamos ver com infra
[14:25:37] EU: faz sentido
[14:25:47] OUTROS: alguém tá ouvindo eco?
[14:25:59] OUTROS: uhum
[14:26:08] EU: acho que é o meu microfone, peraí
[14:26:15] OUTROS: fica em aberto então, sem dono por enquanto
[14:26:27] EU: acho que é o meu microfone, peraí
[14:26:33] EU: acho que é o meu microfone, peraí
[14:26:39] OUTROS: pode ser
[14:26:51] OUTROS: voltou, pode continuar
[14:27:00] OUTROS: mais alguma coisa? Nick, você tinha algo sobre o on-call?
[14:27:07] EU: faz sentido
[14:27:14] OUTROS: pode ser
[14:27:26] EU: acho que é o meu microfone, peraí
[14:27:34] EU: tá
[14:27:45] EU: só lembrar que a escala de on-call de outubro precisa sair até dia primeiro
[14:27:56] OUTROS: pode ser
[14:28:05] OUTROS: pode ser
[14:28:16] EU: tá
[14:28:30] OUTROS: a Júlia monta a escala de on-call até segunda
[14:28:37] OUTROS: certo, certo
[14:28:44] OUTROS: uhum
[14:28:51] OUTROS: alguém tá ouvindo eco?
[14:29:00] EU: acho que é o meu microfone, peraí
[14:29:07] OUTROS: alguém tá ouvindo eco?
[14:29:15] OUTROS: beleza pessoal, obrigado, até amanhã
[14:29:25] EU: faz sentido
[14:29:36] OUTROS: sim
[14:29:43] OUTROS: só um segundo que meu áudio tá cortando
[14:29:53] OUTROS: certo, certo
